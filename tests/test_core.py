import hashlib
import hmac
import io
import json
import sys
import tarfile
import types
import unittest
from unittest import mock

from pr_sentinel.config import Settings
from pr_sentinel.context import NoContext
from pr_sentinel.diff_parser import nearest_commentable, parse_patch
from pr_sentinel.github_client import SUMMARY_MARKER, GitHubClient
from pr_sentinel.service import ReviewService, is_ignored, select_files
from pr_sentinel.webhook import job_from_event, verify_signature

from tests.fakes import PATCH_APP, FakeGitHub, FakeReviewer


def pr_file(name, patch=PATCH_APP, status="modified"):
    return {"filename": name, "status": status, "patch": patch}


class DiffParserTests(unittest.TestCase):
    def test_line_numbers_follow_new_file(self):
        p = parse_patch("app.py", PATCH_APP)
        self.assertEqual(p.added_lines, {
            2: "import subprocess",
            5: '    password = "hunter2"',
            6: "    return subprocess.call(cmd, shell=True)",
        })
        # context lines are commentable too, removed lines are not
        self.assertEqual(p.commentable_lines, {1, 2, 3, 4, 5, 6})
        self.assertIn("    5 +     password", p.annotated)
        self.assertIn("      -     return os.system(cmd)", p.annotated)

    def test_multiple_hunks_and_no_newline_marker(self):
        patch = "@@ -1,1 +1,1 @@\n-a\n+b\n\\ No newline at end of file\n@@ -10,2 +10,3 @@\n x\n+y\n z"
        p = parse_patch("f.txt", patch)
        self.assertEqual(p.added_lines, {1: "b", 11: "y"})
        self.assertEqual(p.commentable_lines, {1, 10, 11, 12})

    def test_nearest_commentable(self):
        self.assertEqual(nearest_commentable(5, {1, 5, 9}), 5)
        self.assertEqual(nearest_commentable(7, {1, 5, 9}), 5)  # tie -> first found closest
        self.assertIsNone(nearest_commentable(50, {1, 5, 9}))


class WebhookTests(unittest.TestCase):
    def test_signature(self):
        body = b'{"a":1}'
        sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_signature("s3cret", body, sig))
        self.assertFalse(verify_signature("s3cret", body, "sha256=bad"))
        self.assertFalse(verify_signature("s3cret", body, None))

    def payload(self, action="opened", draft=False):
        return {
            "action": action,
            "repository": {"full_name": "Krinal09/demo"},
            "installation": {"id": 42},
            "pull_request": {"number": 7, "draft": draft, "state": "open"},
        }

    def test_job_from_event(self):
        job = job_from_event("pull_request", self.payload())
        self.assertEqual((job.repo, job.number, job.installation_id), ("Krinal09/demo", 7, 42))
        self.assertIsNone(job_from_event("pull_request", self.payload(action="closed")))
        self.assertIsNone(job_from_event("pull_request", self.payload(draft=True)))
        self.assertIsNone(job_from_event("push", self.payload()))


class FileSelectionTests(unittest.TestCase):
    def test_ignores_lockfiles_removed_and_binary(self):
        s = Settings()
        files = [pr_file("app.py"), pr_file("package-lock.json"), pr_file("old.py", status="removed"),
                 {"filename": "logo.png", "status": "added"}]
        self.assertEqual([p.path for p in select_files(files, s)], ["app.py"])
        self.assertTrue(is_ignored("dist/x.js", ["dist/*"]))
        self.assertTrue(is_ignored("frontend/node_modules/a/b.js", ["node_modules/*"]))
        self.assertTrue(is_ignored("static/app.min.js", ["*.min.js"]))
        self.assertFalse(is_ignored("src/distance.py", ["dist/*"]))

    def test_max_files(self):
        s = Settings(max_files=2)
        files = [pr_file(f"f{i}.py") for i in range(5)]
        self.assertEqual(len(select_files(files, s)), 2)


class ReviewServiceTests(unittest.TestCase):
    findings = {"app.py": [
        {"line": 5, "severity": "critical", "category": "security",
         "message": "Hard-coded password.", "suggestion": '    password = os.environ["PASSWORD"]'},
        {"line": 6, "severity": "high", "category": "security",
         "message": "shell=True allows command injection."},
        {"line": 6, "severity": "high", "category": "security",
         "message": "shell=True allows command injection."},          # duplicate
        {"line": 99, "severity": "low", "category": "style", "message": "Not in the diff."},
        {"line": 2, "severity": "low", "category": "style", "message": "Unused import?"},
    ]}

    def make(self, files=None, settings=None, **gh_kwargs):
        gh = FakeGitHub(files or [pr_file("app.py")], **gh_kwargs)
        reviewer = FakeReviewer(self.findings)
        svc = ReviewService(gh, reviewer, settings or Settings(), context=NoContext())
        return gh, reviewer, svc

    def test_posts_inline_comments_and_summary(self):
        gh, _, svc = self.make()
        result = svc.review_pull_request("Krinal09/demo", 7)

        self.assertEqual(result.files_reviewed, 1)
        comments = gh.reviews[0]["comments"]
        self.assertEqual([(c["path"], c["line"]) for c in comments],
                         [("app.py", 5), ("app.py", 6), ("app.py", 2)])  # dedup + off-diff dropped
        self.assertIn("```suggestion", comments[0]["body"])
        self.assertEqual(gh.reviews[0]["commit_id"], "abc1234def")
        self.assertIn("## PR Sentinel review", gh.summaries[0])
        self.assertIn("CRITICAL: 1", gh.summaries[0])

    def test_min_severity_filter(self):
        gh, _, svc = self.make(settings=Settings(min_severity="high"))
        svc.review_pull_request("Krinal09/demo", 7)
        self.assertEqual(len(gh.reviews[0]["comments"]), 2)

    def test_falls_back_when_github_rejects_inline_lines(self):
        gh, _, svc = self.make(reject_inline=True)
        svc.review_pull_request("Krinal09/demo", 7)
        self.assertEqual(gh.reviews[0]["comments"], [])
        self.assertIn("`app.py:5`", gh.reviews[0]["body"])

    def test_one_file_failing_does_not_break_the_review(self):
        gh = FakeGitHub([pr_file("app.py"), pr_file("other.py")])
        reviewer = FakeReviewer(self.findings, fail_on={"other.py"})
        ReviewService(gh, reviewer, Settings(), context=NoContext()).review_pull_request("o/r", 1)
        self.assertEqual(sorted(reviewer.calls), ["app.py", "other.py"])
        self.assertEqual(len(gh.reviews[0]["comments"]), 3)

    def test_dry_run_posts_nothing(self):
        gh, _, svc = self.make()
        result = svc.review_pull_request("Krinal09/demo", 7, post=False)
        self.assertEqual((gh.reviews, gh.summaries), ([], []))
        self.assertEqual(len(result.comments), 3)

    def test_nothing_reviewable(self):
        gh, reviewer, svc = self.make(files=[pr_file("yarn.lock")])
        result = svc.review_pull_request("Krinal09/demo", 7)
        self.assertEqual((result.files_reviewed, reviewer.calls, gh.reviews), (0, [], []))


class GitHubClientTests(unittest.TestCase):
    def response(self, json_data=None, content=b"", links=None):
        r = mock.Mock(status_code=200, content=content, links=links or {})
        r.json.return_value = json_data
        return r

    def test_upsert_updates_existing_summary(self):
        gh = GitHubClient("t")
        existing = [{"id": 1, "body": "hi"}, {"id": 2, "body": f"{SUMMARY_MARKER}\nold"}]
        with mock.patch.object(gh, "_request", side_effect=[self.response(existing), self.response({})]) as req:
            gh.upsert_summary_comment("o/r", 3, "new")
        method, path = req.call_args_list[1].args
        self.assertEqual((method, path), ("PATCH", "/repos/o/r/issues/comments/2"))

    def test_upsert_creates_when_missing(self):
        gh = GitHubClient("t")
        with mock.patch.object(gh, "_request", side_effect=[self.response([]), self.response({})]) as req:
            gh.upsert_summary_comment("o/r", 3, "new")
        self.assertEqual(req.call_args_list[1].args, ("POST", "/repos/o/r/issues/3/comments"))

    def test_tarball_download_filters_files(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, data in {"o-r-abc/src/a.py": b"print(1)", "o-r-abc/img.png": b"\x89PNG",
                               "o-r-abc/big.py": b"x" * 100}.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        gh = GitHubClient("t")
        with mock.patch.object(gh, "_request", return_value=self.response(content=buf.getvalue())):
            files = gh.download_repo_files("o/r", "abc", max_files=10, max_bytes=50, extensions=(".py",))
        self.assertEqual(files, {"src/a.py": "print(1)"})


class LambdaTests(unittest.TestCase):
    def setUp(self):
        self.fake_boto3 = types.SimpleNamespace(client=mock.Mock())
        sys.modules["boto3"] = self.fake_boto3
        from pr_sentinel import lambda_handler
        self.lh = lambda_handler
        self.lh.settings = Settings(github_webhook_secret="")

    def tearDown(self):
        sys.modules.pop("boto3", None)

    def test_webhook_queues_async_job(self):
        body = json.dumps(WebhookTests().payload())
        event = {"headers": {"X-GitHub-Event": "pull_request"}, "body": body}
        resp = self.lh.handler(event, types.SimpleNamespace(function_name="pr-sentinel"))
        self.assertEqual(resp["statusCode"], 202)
        kwargs = self.fake_boto3.client.return_value.invoke.call_args.kwargs
        self.assertEqual(kwargs["InvocationType"], "Event")
        self.assertEqual(json.loads(kwargs["Payload"])["pr_sentinel_job"]["number"], 7)

    def test_job_event_runs_review(self):
        with mock.patch.object(self.lh, "run_job") as run:
            self.lh.handler({"pr_sentinel_job": {"repo": "o/r", "number": 1, "installation_id": 9}}, None)
        self.assertEqual(run.call_args.args[0].installation_id, 9)

    def test_bad_signature_rejected(self):
        self.lh.settings = Settings(github_webhook_secret="x")
        resp = self.lh.handler({"headers": {}, "body": "{}"}, None)
        self.assertEqual(resp["statusCode"], 401)


try:
    import fastapi  # noqa: F401
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False


@unittest.skipUnless(HAS_FASTAPI, "fastapi not installed")
class FastAPITests(unittest.TestCase):
    def test_health_and_ignored_event(self):
        from fastapi.testclient import TestClient

        from pr_sentinel import main
        main.settings.github_webhook_secret = ""
        client = TestClient(main.app)
        self.assertEqual(client.get("/health").json()["status"], "ok")
        r = client.post("/webhook", json={"action": "closed"}, headers={"X-GitHub-Event": "pull_request"})
        self.assertEqual(r.json(), {"status": "ignored"})


if __name__ == "__main__":
    unittest.main()
