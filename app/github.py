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

    async def issue_has_comment(self, issue_number: int, marker: str) -> bool:
        """True when an issue comment already contains `marker`.

        Lets a caller retry a failed comment without risking a duplicate when
        the request actually reached GitHub.
        """
        url = f"https://api.github.com/repos/{self._cfg.target_repository}/issues/{issue_number}/comments"
        page = 1
        while page <= 10:
            resp = await self._client.get(
                url, headers=self._headers, params={"per_page": 100, "page": page}
            )
            resp.raise_for_status()
            items = resp.json()
            if not isinstance(items, list) or not items:
                return False
            for comment in items:
                if marker in (comment.get("body") or ""):
                    return True
            if len(items) < 100:
                return False
            page += 1
        return False

    async def pr_status(self, pr_url: str) -> Optional[dict[str, Any]]:
        """Return {state, merged, url_repo, base_repo, body} for a PR URL, or None.

        `url_repo` is the owner/repo parsed from the URL; `base_repo` is the
        repository the PR actually targets per the API. Callers must confirm both
        equal TARGET_REPOSITORY before trusting the PR.
        """
        m = _PR_URL_RE.search(pr_url or "")
        if not m:
            return None
        api = f"https://api.github.com/repos/{m['repo']}/pulls/{m['number']}"
        resp = await self._client.get(api, headers=self._headers)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        base = data.get("base") or {}
        base_repo = ((base.get("repo") or {}).get("full_name")) or ""
        return {
            "state": data.get("state"),
            "merged": bool(data.get("merged")),
            "url_repo": m["repo"],
            "base_repo": base_repo,
            "base_ref": base.get("ref") or "",
            "body": data.get("body") or "",
        }

    async def default_branch(self, repo: str) -> Optional[str]:
        resp = await self._client.get(
            f"https://api.github.com/repos/{repo}", headers=self._headers
        )
        if resp.status_code >= 400:
            return None
        return resp.json().get("default_branch")
