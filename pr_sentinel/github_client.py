"""Thin GitHub REST API client.

Supports two auth modes:
  * GitHub App  -> short-lived installation token (webhook server / Lambda)
  * Plain token -> GITHUB_TOKEN inside GitHub Actions, or a PAT for local runs
"""
from __future__ import annotations

import io
import logging
import tarfile
import time
from typing import Any, Iterator

import jwt
import requests

log = logging.getLogger(__name__)

SUMMARY_MARKER = "<!-- pr-sentinel-summary -->"


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


def create_app_jwt(app_id: str, private_key: str) -> str:
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": str(app_id)}
    return jwt.encode(payload, private_key, algorithm="RS256")


def get_installation_token(app_id: str, private_key: str, installation_id: int,
                           api_url: str = "https://api.github.com") -> str:
    resp = requests.post(
        f"{api_url}/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {create_app_jwt(app_id, private_key)}",
            "Accept": "application/vnd.github+json",
        },
        timeout=20,
    )
    if resp.status_code >= 300:
        raise GitHubError(resp.status_code, resp.text)
    return resp.json()["token"]


class GitHubClient:
    def __init__(self, token: str, api_url: str = "https://api.github.com"):
        self.api_url = api_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pr-sentinel",
        })

    # ---------- low level ----------
    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = path if path.startswith("http") else f"{self.api_url}{path}"
        resp = self.session.request(method, url, timeout=kwargs.pop("timeout", 30), **kwargs)
        if resp.status_code >= 300:
            raise GitHubError(resp.status_code, resp.text[:500])
        return resp

    def _paginate(self, path: str, params: dict | None = None) -> Iterator[dict]:
        params = {"per_page": 100, **(params or {})}
        url: str | None = path
        while url:
            resp = self._request("GET", url, params=params)
            yield from resp.json()
            url = resp.links.get("next", {}).get("url")
            params = None  # the "next" URL already carries the query string

    # ---------- pull requests ----------
    def get_pull(self, repo: str, number: int) -> dict:
        return self._request("GET", f"/repos/{repo}/pulls/{number}").json()

    def list_pull_files(self, repo: str, number: int) -> list[dict]:
        return list(self._paginate(f"/repos/{repo}/pulls/{number}/files"))

    def create_review(self, repo: str, number: int, commit_id: str, body: str,
                      comments: list[dict]) -> dict:
        payload = {"commit_id": commit_id, "body": body, "event": "COMMENT", "comments": comments}
        return self._request("POST", f"/repos/{repo}/pulls/{number}/reviews", json=payload).json()

    def upsert_summary_comment(self, repo: str, number: int, body: str) -> None:
        """Keep a single summary comment per PR and update it on every push."""
        body = f"{SUMMARY_MARKER}\n{body}"
        for comment in self._paginate(f"/repos/{repo}/issues/{number}/comments"):
            if SUMMARY_MARKER in (comment.get("body") or ""):
                self._request("PATCH", f"/repos/{repo}/issues/comments/{comment['id']}",
                              json={"body": body})
                return
        self._request("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})

    # ---------- repository contents ----------
    def download_repo_files(self, repo: str, ref: str, max_files: int,
                            max_bytes: int = 60_000,
                            extensions: tuple[str, ...] = ()) -> dict[str, str]:
        """Fetch the whole repo at `ref` as one tarball (1 API call instead of hundreds)."""
        resp = self._request("GET", f"/repos/{repo}/tarball/{ref}", timeout=120)
        files: dict[str, str] = {}
        with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
            for member in tar:
                if len(files) >= max_files:
                    break
                if not member.isfile() or member.size > max_bytes:
                    continue
                path = member.name.split("/", 1)[-1]  # strip "<owner>-<repo>-<sha>/"
                if extensions and not path.endswith(extensions):
                    continue
                fh = tar.extractfile(member)
                if fh is None:
                    continue
                try:
                    files[path] = fh.read().decode("utf-8")
                except UnicodeDecodeError:
                    continue
        log.info("Downloaded %d files from %s@%s", len(files), repo, ref[:7])
        return files
