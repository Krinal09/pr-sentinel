"""Webhook helpers shared by the FastAPI server and the AWS Lambda handler."""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

REVIEW_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}


@dataclass
class ReviewJob:
    repo: str
    number: int
    installation_id: int | None


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not secret:
        return True  # signature check disabled (local testing only!)
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def job_from_event(event: str | None, payload: dict) -> ReviewJob | None:
    """Return a job if this webhook should trigger a review, else None."""
    if event != "pull_request" or payload.get("action") not in REVIEW_ACTIONS:
        return None
    pr = payload.get("pull_request") or {}
    if pr.get("draft") or pr.get("state") != "open":
        return None
    return ReviewJob(
        repo=payload["repository"]["full_name"],
        number=int(pr["number"]),
        installation_id=(payload.get("installation") or {}).get("id"),
    )


def run_job(job: ReviewJob, settings) -> None:
    from .github_client import get_installation_token
    from .service import build_service

    if job.installation_id and settings.github_app_id:
        token = get_installation_token(settings.github_app_id, settings.github_private_key,
                                       job.installation_id, settings.github_api_url)
    else:
        token = settings.github_token
    if not token:
        raise RuntimeError("No GitHub credentials: configure the GitHub App or GITHUB_TOKEN")
    build_service(token, settings).review_pull_request(job.repo, job.number)
