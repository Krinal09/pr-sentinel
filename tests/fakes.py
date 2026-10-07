"""In-memory fakes so the review pipeline can be tested without GitHub or an LLM."""
from pr_sentinel.github_client import GitHubError
from pr_sentinel.reviewer import FileReview, Finding, PRSummary

PATCH_APP = """@@ -1,4 +1,6 @@
 import os
+import subprocess

 def run(cmd):
-    return os.system(cmd)
+    password = "hunter2"
+    return subprocess.call(cmd, shell=True)
"""


class FakeGitHub:
    def __init__(self, files, reject_inline=False):
        self.files = files
        self.reject_inline = reject_inline
        self.reviews = []
        self.summaries = []

    def get_pull(self, repo, number):
        return {"title": "Add runner", "body": "Runs commands", "head": {"sha": "abc1234def"}}

    def list_pull_files(self, repo, number):
        return self.files

    def upsert_summary_comment(self, repo, number, body):
        self.summaries.append(body)

    def create_review(self, repo, number, commit_id, body, comments):
        if comments and self.reject_inline:
            raise GitHubError(422, "Line could not be resolved")
        self.reviews.append({"commit_id": commit_id, "body": body, "comments": comments})
        return {"id": 1}


class FakeReviewer:
    def __init__(self, findings_by_path=None, fail_on=()):
        self.findings_by_path = findings_by_path or {}
        self.fail_on = set(fail_on)
        self.calls = []

    def review_file(self, *, title, description, path, diff, context):
        self.calls.append(path)
        if path in self.fail_on:
            raise RuntimeError("LLM timeout")
        return FileReview(findings=[Finding(**f) for f in self.findings_by_path.get(path, [])])

    def summarize(self, *, title, description, files, findings):
        return PRSummary(overview="Adds a command runner.", key_changes=["New run()"], risk="high")
