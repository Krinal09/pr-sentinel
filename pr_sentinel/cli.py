"""Command-line entry point.

Inside GitHub Actions (no arguments needed):
    python -m pr_sentinel.cli

Locally against any PR (needs GITHUB_TOKEN + GROQ_API_KEY):
    python -m pr_sentinel.cli --repo owner/name --pr 12 --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .config import Settings
from .service import build_service


def _from_actions_event() -> tuple[str, int] | None:
    path = os.getenv("GITHUB_EVENT_PATH")
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        event = json.load(fh)
    pr = event.get("pull_request")
    if not pr:
        return None
    return event["repository"]["full_name"], int(pr["number"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pr-sentinel", description="AI pull request reviewer")
    parser.add_argument("--repo", help="owner/name (defaults to the GitHub Actions event)")
    parser.add_argument("--pr", type=int, help="pull request number")
    parser.add_argument("--dry-run", action="store_true", help="print the review, don't post it")
    parser.add_argument("--no-rag", action="store_true", help="skip repository context")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()

    target = (args.repo, args.pr) if args.repo and args.pr else _from_actions_event()
    if not target:
        parser.error("pass --repo and --pr, or run inside a pull_request workflow")
    if not settings.github_token:
        parser.error("GITHUB_TOKEN is not set")

    repo, number = target
    service = build_service(settings.github_token, settings, use_rag=not args.no_rag)
    result = service.review_pull_request(repo, number, post=not args.dry_run)

    if args.dry_run:
        print(result.summary_markdown or "Nothing to review.")
        for c in result.comments:
            print(f"\n--- {c.path}:{c.line}\n{c.to_github()['body']}")
    print(f"\nReviewed {result.files_reviewed} file(s), {len(result.comments)} finding(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
