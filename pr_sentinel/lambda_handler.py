"""AWS Lambda entry point (API Gateway HTTP API -> Lambda).

GitHub gives a webhook 10 seconds to respond, but an LLM review takes longer.
So the function handles two kinds of events:
  1. HTTP webhook from API Gateway -> verify, re-invoke itself asynchronously, return 202
  2. The async "review job" event   -> run the actual review
Logs go to CloudWatch automatically.
"""
from __future__ import annotations

import base64
import json
import logging
import os

from .config import Settings
from .webhook import ReviewJob, job_from_event, run_job, verify_signature

log = logging.getLogger()
log.setLevel(logging.INFO)

settings = Settings.from_env()


def _response(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body)}


def handler(event: dict, context) -> dict:
    # (2) Async job we sent to ourselves.
    if "pr_sentinel_job" in event:
        job = ReviewJob(**event["pr_sentinel_job"])
        run_job(job, settings)
        return {"status": "done"}

    # (1) Webhook from API Gateway.
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    raw = event.get("body") or ""
    body = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode()

    if not verify_signature(settings.github_webhook_secret, body, headers.get("x-hub-signature-256")):
        return _response(401, {"error": "invalid signature"})

    job = job_from_event(headers.get("x-github-event"), json.loads(body or b"{}"))
    if job is None:
        return _response(200, {"status": "ignored"})

    import boto3  # available in the Lambda runtime

    boto3.client("lambda").invoke(
        FunctionName=os.environ.get("AWS_LAMBDA_FUNCTION_NAME", context.function_name),
        InvocationType="Event",
        Payload=json.dumps({"pr_sentinel_job": job.__dict__}).encode(),
    )
    log.info("Queued review for %s#%s", job.repo, job.number)
    return _response(202, {"status": "queued", "repo": job.repo, "pr": job.number})
