"""Orchestrates one full review of a pull request."""
from __future__ import annotations

import fnmatch
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .config import SEVERITY_ORDER, Settings
from .context import ContextProvider, NoContext, build_context
from .diff_parser import ParsedPatch, nearest_commentable, parse_patch
from .github_client import GitHubClient, GitHubError
from .reviewer import Finding, Reviewer

log = logging.getLogger(__name__)

SEVERITY_LABEL = {"critical": "CRITICAL", "high": "HIGH", "medium": "MEDIUM", "low": "LOW"}


@dataclass
class InlineComment:
    path: str
    line: int
    finding: Finding

    def to_github(self) -> dict:
        f = self.finding
        body = f"**[{SEVERITY_LABEL[f.severity]}] {f.category.title()}**: {f.message}"
        if f.suggestion.strip():
            body += f"\n\n```suggestion\n{f.suggestion.rstrip()}\n```"
        return {"path": self.path, "line": self.line, "side": "RIGHT", "body": body}


@dataclass
class ReviewResult:
    files_reviewed: int
    comments: list[InlineComment]
    summary_markdown: str


def is_ignored(path: str, patterns: list[str]) -> bool:
    """Match the full path and every sub-path, so "dist/*" also hits "web/dist/app.js"."""
    parts = path.split("/")
    candidates = ["/".join(parts[i:]) for i in range(len(parts))]
    return any(fnmatch.fnmatch(c, p) for c in candidates for p in patterns)


def select_files(files: list[dict], settings: Settings) -> list[ParsedPatch]:
    selected: list[ParsedPatch] = []
    for f in files:
        if f.get("status") == "removed" or not f.get("patch"):
            continue  # deleted, binary, or too large for GitHub to render a patch
        if is_ignored(f["filename"], settings.ignore_patterns):
            continue
        parsed = parse_patch(f["filename"], f["patch"][: settings.max_patch_chars])
        if parsed.added_lines:
            selected.append(parsed)
    # Review the biggest changes first if we have to cut.
    selected.sort(key=lambda p: len(p.added_lines), reverse=True)
    return selected[: settings.max_files]


def map_findings(patch: ParsedPatch, findings: list[Finding], min_severity: str) -> list[InlineComment]:
    threshold = SEVERITY_ORDER.get(min_severity, 0)
    comments, seen = [], set()
    for f in findings:
        if SEVERITY_ORDER[f.severity] < threshold:
            continue
        line = nearest_commentable(f.line, patch.commentable_lines)
        if line is None:
            log.debug("Dropping finding on non-diff line %s:%s", patch.path, f.line)
            continue
        key = (line, f.message.strip().lower())
        if key in seen:
            continue
        seen.add(key)
        comments.append(InlineComment(patch.path, line, f))
    return comments


def render_summary(summary, comments: list[InlineComment], files_reviewed: int) -> str:
    counts = {s: 0 for s in SEVERITY_LABEL}
    for c in comments:
        counts[c.finding.severity] += 1
    table = " | ".join(f"{SEVERITY_LABEL[s]}: {n}" for s, n in counts.items())
    lines = [
        "## PR Sentinel review",
        "",
        summary.overview,
        "",
        "**Key changes**",
        *[f"- {c}" for c in summary.key_changes],
        "",
        f"**Risk:** {summary.risk.upper()}  ·  **Files reviewed:** {files_reviewed}  ·  "
        f"**Findings:** {len(comments)} ({table})",
    ]
    return "\n".join(lines)


class ReviewService:
    def __init__(self, gh: GitHubClient, reviewer: Reviewer, settings: Settings,
                 context: ContextProvider | None = None, workers: int = 4):
        self.gh = gh
        self.reviewer = reviewer
        self.settings = settings
        self.context = context
        self.workers = workers

    def review_pull_request(self, repo: str, number: int, post: bool = True) -> ReviewResult:
        pr = self.gh.get_pull(repo, number)
        head_sha = pr["head"]["sha"]
        title, description = pr.get("title", ""), (pr.get("body") or "")[:3000]
        log.info("Reviewing %s#%s @ %s", repo, number, head_sha[:7])

        patches = select_files(self.gh.list_pull_files(repo, number), self.settings)
        if not patches:
            log.info("Nothing reviewable in %s#%s", repo, number)
            return ReviewResult(0, [], "")

        context = self.context or build_context(self.gh, repo, head_sha, self.settings)

        def review_one(patch: ParsedPatch) -> list[InlineComment]:
            query = patch.path + "\n" + "\n".join(patch.added_lines.values())
            try:
                result = self.reviewer.review_file(
                    title=title, description=description, path=patch.path,
                    diff=patch.annotated, context=context.related_context(patch.path, query),
                )
            except Exception as exc:
                log.warning("LLM review failed for %s: %s", patch.path, exc)
                return []
            return map_findings(patch, result.findings, self.settings.min_severity)

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            comments = [c for batch in pool.map(review_one, patches) for c in batch]

        findings_text = "\n".join(
            f"- {c.path}:{c.line} [{c.finding.severity}] {c.finding.message}" for c in comments
        )
        files_text = "\n".join(f"- {p.path} (+{len(p.added_lines)} lines)" for p in patches)
        summary = self.reviewer.summarize(title=title, description=description,
                                          files=files_text, findings=findings_text)
        summary_md = render_summary(summary, comments, len(patches))

        if post:
            self._publish(repo, number, head_sha, summary_md, comments)
        return ReviewResult(len(patches), comments, summary_md)

    def _publish(self, repo: str, number: int, sha: str, summary_md: str,
                 comments: list[InlineComment]) -> None:
        self.gh.upsert_summary_comment(repo, number, summary_md)
        if not comments:
            return
        body = f"PR Sentinel left {len(comments)} inline comment(s)."
        try:
            self.gh.create_review(repo, number, sha, body, [c.to_github() for c in comments])
        except GitHubError as exc:
            if exc.status != 422:
                raise
            # A line GitHub refuses (e.g. diff changed under us): fall back to one plain review.
            log.warning("Inline comments rejected (%s); posting them in the review body", exc)
            listing = "\n".join(
                f"- `{c.path}:{c.line}` **[{SEVERITY_LABEL[c.finding.severity]}]** {c.finding.message}"
                for c in comments
            )
            self.gh.create_review(repo, number, sha, f"{body}\n\n{listing}", [])


def build_service(token: str, settings: Settings, use_rag: bool | None = None) -> ReviewService:
    from .reviewer import LLMReviewer

    gh = GitHubClient(token, settings.github_api_url)
    reviewer = LLMReviewer(settings.llm_provider, settings.llm_model, settings.llm_temperature)
    context = None if (use_rag if use_rag is not None else settings.enable_rag) else NoContext()
    return ReviewService(gh, reviewer, settings, context=context)
