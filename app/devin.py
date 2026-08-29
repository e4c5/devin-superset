"""Devin v3 organization-scoped API client.

Only the endpoints Ops Guard needs:
  POST   /v3/organizations/{org}/sessions
  GET    /v3/organizations/{org}/sessions/{id}
  GET    /v3/organizations/{org}/sessions?tags=...
  POST   /v3/organizations/{org}/sessions/{id}/messages
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

from .config import Config


class DevinError(RuntimeError):
    def __init__(self, message: str, *, status_code: Optional[int] = None, retry_after: Optional[float] = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class DevinClient:
    def __init__(self, cfg: Config, client: Optional[httpx.AsyncClient] = None):
        self._cfg = cfg
        self._base = f"{cfg.devin_api_base}/v3/organizations/{cfg.devin_org_id}"
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._cfg.devin_token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _retry_after(resp: httpx.Response) -> Optional[float]:
        raw = resp.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    def _raise(self, resp: httpx.Response) -> None:
        raise DevinError(
            f"{resp.request.method} {resp.request.url.path} -> {resp.status_code}: {resp.text[:500]}",
            status_code=resp.status_code,
            retry_after=self._retry_after(resp),
        )

    async def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        resp = await self._client.post(
            f"{self._base}/sessions", headers=self._headers, json=payload
        )
        if resp.status_code >= 400:
            self._raise(resp)
        return resp.json()

    async def get_session(self, session_id: str) -> dict[str, Any]:
        resp = await self._client.get(
            f"{self._base}/sessions/{session_id}", headers=self._headers
        )
        if resp.status_code >= 400:
            self._raise(resp)
        return resp.json()

    async def list_sessions_by_tag(self, tag: str, *, max_pages: int = 10) -> list[dict[str, Any]]:
        """List org sessions carrying `tag`, following pagination.

        The v3 List Sessions response is paginated as
        ``{"items": [...], "end_cursor": <str|null>, "has_next_page": <bool>}``.
        Because the server-side `tags` filter is not contractually guaranteed, we
        also filter client-side on each session's own `tags`.
        """
        out: list[dict[str, Any]] = []
        cursor: Optional[str] = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"tags": tag, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            resp = await self._client.get(
                f"{self._base}/sessions", headers=self._headers, params=params
            )
            if resp.status_code >= 400:
                self._raise(resp)
            body = resp.json()
            if isinstance(body, list):
                out.extend(body)
                break
            page = body.get("items") or body.get("sessions") or body.get("data") or []
            out.extend(page)
            cursor = body.get("end_cursor") or body.get("next_cursor")
            if not body.get("has_next_page") or not cursor:
                break

        # Require a definite tag match. A session whose `tags` are absent or not
        # a list is NOT trusted as this job's session — reconciliation would
        # rather find nothing (and retry / escalate) than attach the wrong one.
        def _has_tag(s: dict[str, Any]) -> bool:
            tags = s.get("tags")
            return isinstance(tags, list) and tag in tags

        return [s for s in out if _has_tag(s)]

    async def send_message(self, session_id: str, message: str) -> None:
        resp = await self._client.post(
            f"{self._base}/sessions/{session_id}/messages",
            headers=self._headers,
            json={"message": message},
        )
        if resp.status_code >= 400:
            self._raise(resp)


def build_create_payload(cfg: Config, job: dict[str, Any], issue_body: str) -> dict[str, Any]:
    n = job["issue_number"]
    prompt = (
        f"You are remediating GitHub issue #{n} in {job['issue_url'].rsplit('/issues/', 1)[0]}.\n\n"
        f"Issue title: {job['title']}\n"
        f"Issue body:\n{issue_body}\n\n"
        "Work only on this issue. Inspect the existing code and tests before editing. "
        "Implement the smallest correct fix, add or update focused tests, run the issue's "
        "stated test command, then open a pull request that targets the fork's default "
        f"branch and whose description contains a closing reference `Fixes #{n}`. "
        "In your final structured output report the PR URL, outcome, a concise summary, the "
        "tests you ran, and any blocker. Do not broaden the change or modify unrelated "
        "dependencies."
    )
    payload: dict[str, Any] = {
        "prompt": prompt,
        "title": f"Ops Guard: {cfg.target_repository} issue #{n}",
        "repos": [cfg.devin_repository_id],
        "tags": ["ops-guard", job["correlation_tag"]],
        "max_acu_limit": cfg.max_acu_limit,
        "resumable": False,
        "structured_output_required": True,
        "structured_output_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["outcome", "summary", "tests_run", "pr_url", "blocker"],
            "properties": {
                "outcome": {
                    "type": "string",
                    "enum": ["remediated", "blocked", "not_reproducible"],
                },
                "summary": {"type": "string"},
                "tests_run": {"type": "array", "items": {"type": "string"}},
                "pr_url": {"type": ["string", "null"]},
                "blocker": {"type": ["string", "null"]},
            },
        },
    }
    if cfg.bypass_approval:
        payload["bypass_approval"] = True
    return payload
