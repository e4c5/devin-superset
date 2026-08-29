"""GitHub webhook verification and event parsing."""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Optional

from .config import Config


def verify_signature(secret: str, raw_body: bytes, signature_header: Optional[str]) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@dataclass(frozen=True)
class TriggerEvent:
    repository_full_name: str
    repository_id: str
    issue_number: int
    issue_url: str
    title: str
    action: str


class WebhookDecision:
    ACCEPT = "accept"
    IGNORE = "ignore"
    REJECT_REPO = "reject_repo"

    def __init__(self, kind: str, event: Optional[TriggerEvent] = None, reason: str = ""):
        self.kind = kind
        self.event = event
        self.reason = reason


def evaluate(cfg: Config, gh_event: str, payload: dict[str, Any]) -> WebhookDecision:
    if gh_event != "issues":
        return WebhookDecision(WebhookDecision.IGNORE, reason=f"event {gh_event!r} not handled")

    repo = payload.get("repository") or {}
    full_name = repo.get("full_name", "")
    if full_name != cfg.target_repository:
        return WebhookDecision(
            WebhookDecision.REJECT_REPO, reason=f"repository {full_name!r} not allowlisted"
        )

    action = payload.get("action", "")
    issue = payload.get("issue")
    if not isinstance(issue, dict):
        return WebhookDecision(WebhookDecision.IGNORE, reason="payload has no issue object")
    labels = {
        lbl.get("name")
        for lbl in (issue.get("labels") or [])
        if isinstance(lbl, dict)
    }

    triggered = False
    if action == "labeled":
        triggered = (payload.get("label") or {}).get("name") == cfg.autofix_label
    elif action == "opened":
        triggered = cfg.autofix_label in labels

    if not triggered:
        return WebhookDecision(
            WebhookDecision.IGNORE, reason=f"action {action!r} without {cfg.autofix_label} trigger"
        )

    raw_number = issue.get("number")
    try:
        issue_number = int(raw_number)
    except (TypeError, ValueError):
        return WebhookDecision(
            WebhookDecision.IGNORE, reason=f"issue.number is not an integer: {raw_number!r}"
        )
    if issue_number <= 0:
        return WebhookDecision(WebhookDecision.IGNORE, reason=f"issue.number out of range: {issue_number}")

    issue_url = issue.get("html_url")
    title = issue.get("title")
    if not isinstance(issue_url, str) or not isinstance(title, str):
        return WebhookDecision(WebhookDecision.IGNORE, reason="issue missing html_url or title")

    event = TriggerEvent(
        repository_full_name=full_name,
        repository_id=full_name,
        issue_number=issue_number,
        issue_url=issue_url,
        title=title,
        action=action,
    )
    return WebhookDecision(WebhookDecision.ACCEPT, event=event)
