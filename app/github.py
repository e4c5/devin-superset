"""Minimal GitHub REST client: issue comments + PR state reads.

This token never grants Devin write access. Devin opens PRs through its own
org-level Git connection.
"""
from __future__ import annotations

import re
from typing import Any, Optional

import httpx

from .config import Config

_PR_URL_RE = re.compile(r"github\.com/(?P<repo>[^/]+/[^/]+)/pull/(?P<number>\d+)")


class GitHubClient:
    def __init__(self, cfg: Config, client: Optional[httpx.AsyncClient] = None):
        self._cfg = cfg
        self._client = client or httpx.AsyncClient(timeout=20.0)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._cfg.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def comment_on_issue(self, issue_number: int, body: str) -> None:
        url = f"https://api.github.com/repos/{self._cfg.target_repository}/issues/{issue_number}/comments"
        resp = await self._client.post(url, headers=self._headers, json={"body": body})
        resp.raise_for_status()

    async def pr_status(self, pr_url: str) -> Optional[dict[str, Any]]:
        """Return {'state': 'open'|'closed', 'merged': bool} for a PR html_url, or None."""
        m = _PR_URL_RE.search(pr_url or "")
        if not m:
            return None
        api = f"https://api.github.com/repos/{m['repo']}/pulls/{m['number']}"
        resp = await self._client.get(api, headers=self._headers)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        return {"state": data.get("state"), "merged": bool(data.get("merged"))}
