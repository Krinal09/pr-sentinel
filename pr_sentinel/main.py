"""FastAPI webhook server for the PR Sentinel GitHub App.

Run locally:  uvicorn pr_sentinel.main:app --reload --port 8000
"""
from __future__ import annotations

import logging

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from .config import Settings
from .webhook import job_from_event, run_job, verify_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pr_sentinel")

settings = Settings.from_env()
app = FastAPI(title="PR Sentinel", version="1.0.0",
              description="AI reviewer for GitHub pull requests")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model": f"{settings.llm_provider}:{settings.llm_model}"}


@app.post("/webhook", status_code=202)
async def webhook(
    request: Request,
    background: BackgroundTasks,
    x_github_event: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
) -> dict:
    body = await request.body()
    if not verify_signature(settings.github_webhook_secret, body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid signature")

    job = job_from_event(x_github_event, await request.json())
    if job is None:
        return {"status": "ignored"}

    # GitHub expects a reply within 10s, so the actual review runs in the background.
    background.add_task(_safe_run, job)
    return {"status": "queued", "repo": job.repo, "pr": job.number}


def _safe_run(job) -> None:
    try:
        run_job(job, settings)
    except Exception:
        log.exception("Review failed for %s#%s", job.repo, job.number)
