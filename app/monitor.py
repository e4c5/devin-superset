"""Session monitor: polls each non-terminal session and verifies completion.

`status: exit` means the session ended, NOT that a fix was delivered. A run is
`remediated` only when the session exited, the structured output says
`remediated`, a PR URL is present, and GitHub independently confirms that PR is
open or merged.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from . import db, states
from .config import Config
from .devin import DevinClient, DevinError
from .github import GitHubClient
from .logging_utils import event

log = logging.getLogger("ops_guard.monitor")

MIN_BACKOFF = 15.0
MAX_BACKOFF = 60.0

# Devin v3 session model: coarse `status` + finer `status_detail`.
#   status:        new | claimed | running | exit | error | suspended | resuming
#   status_detail: working | waiting_for_user | waiting_for_approval | finished
#                  | inactivity | user_request | <billing/limit reasons> | error
_STATUS_WORKING = {"new", "claimed", "running", "resuming"}
_WAITING_DETAILS = {"waiting_for_user", "waiting_for_approval"}
_BILLING_DETAILS = {
    "usage_limit_exceeded", "out_of_credits", "out_of_quota", "no_quota_allocation",
    "payment_declined", "org_usage_limit_exceeded", "user_usage_limit_exceeded",
    "total_session_limit_exceeded",
}


def _extract_pr_url(session: dict[str, Any], structured: Optional[dict]) -> Optional[str]:
    if structured and structured.get("pr_url"):
        return structured["pr_url"]
    prs = session.get("pull_requests") or session.get("pull_request") or []
    if isinstance(prs, dict):
        prs = [prs]
    for pr in prs:
        url = pr.get("pr_url") or pr.get("url") or pr.get("html_url")
        if url:
            return url
    return None


async def _verify_pr_open(cfg: Config, gh: GitHubClient, pr_url: Optional[str],
                          issue_number: int) -> bool:
    """A PR only counts if it targets TARGET_REPOSITORY and is open or merged.
    A `remediated` URL pointing at any other repo is rejected."""
    if not pr_url:
        return False
    try:
        status = await gh.pr_status(pr_url)
    except Exception as exc:  # noqa: BLE001
        log.warning("PR verification failed for %s: %s", pr_url, exc)
        return False
    if not status:
        return False
    target = cfg.target_repository.lower()
    if status["url_repo"].lower() != target or (status["base_repo"] or target).lower() != target:
        log.warning("PR %s targets %s / %s, not %s — rejecting",
                    pr_url, status["url_repo"], status["base_repo"], cfg.target_repository)
        return False
    if str(issue_number) not in (status.get("body") or ""):
        log.warning("PR %s body does not reference issue #%s", pr_url, issue_number)
    return status["state"] == "open" or status["merged"]


async def _evaluate(cfg: Config, gh: GitHubClient, job: dict, session: dict[str, Any]) -> None:
    job_id = job["id"]
    status = (session.get("status") or "").lower()
    detail = (session.get("status_detail") or "").lower()
    acus = session.get("acus_consumed")
    structured = session.get("structured_output")
    if isinstance(structured, str):
        structured = None
    pr_url = _extract_pr_url(session, structured if isinstance(structured, dict) else None)

    db.record_poll(
        job_id,
        status=status or None,
        status_detail=detail or None,
        acus_consumed=float(acus) if isinstance(acus, (int, float)) else None,
        pr_url=pr_url,
        structured_output=structured,
    )

    outcome = structured.get("outcome") if isinstance(structured, dict) else None

    # Errors / billing / usage limits -> failed.
    if status == "error" or detail == "error":
        await _finish(gh, job, states.FAILED, status,
                      f"Devin session errored ({detail or status}).")
        return
    if status == "suspended" or detail in _BILLING_DETAILS:
        await _finish(gh, job, states.FAILED, status,
                      f"Devin session suspended ({detail or status}). Session: {job.get('session_url')}")
        return

    # Paused waiting on a human (session `status` stays `running`).
    if detail in _WAITING_DETAILS:
        await _maybe_nudge(cfg, job, session, detail)
        await _finish(gh, job, states.NEEDS_REVIEW, status,
                      f"Devin is {detail.replace('_', ' ')} — needs a human. "
                      f"Session: {job.get('session_url')}")
        return

    # Still working -> keep polling.
    if status in _STATUS_WORKING or status == "":
        return

    # status == "exit": the session ended. Ended != succeeded.
    if outcome == "remediated" and await _verify_pr_open(cfg, gh, pr_url, job["issue_number"]):
        tests = ", ".join((structured or {}).get("tests_run") or []) or "n/a"
        await _finish(
            gh, job, states.REMEDIATED, status,
            f"Remediated. PR: {pr_url}\nTests run: {tests}\n"
            f"ACUs: {acus if acus is not None else 'n/a'}",
            pr_url=pr_url,
        )
        return
    if outcome in ("blocked", "not_reproducible"):
        blocker = (structured or {}).get("blocker") or outcome
        await _finish(gh, job, states.NEEDS_REVIEW, status,
                      f"Devin reported `{outcome}`: {blocker}\nSession: {job.get('session_url')}")
        return
    await _finish(
        gh, job, states.NEEDS_REVIEW, status,
        f"Session ended ({detail or 'no detail'}) without a verified PR and `remediated` outcome.\n"
        f"Session: {job.get('session_url')}",
    )


async def _finish(gh: GitHubClient, job: dict, state: str, status: str, comment: str,
                  pr_url: Optional[str] = None) -> None:
    db.finalize(job["id"], state=state, status=status, pr_url=pr_url,
                error=None if state == states.REMEDIATED else comment)
    event(log, "job_finalized", job_id=job["id"], state=state, status=status,
          issue=job["issue_number"])
    try:
        await gh.comment_on_issue(job["issue_number"], f"**Ops Guard — {state}**\n\n{comment}")
    except Exception as exc:  # noqa: BLE001
        log.warning("terminal comment failed for #%s: %s", job["issue_number"], exc)


async def _maybe_nudge(cfg: Config, job: dict, session: dict, status: str) -> None:
    """Only a narrowly-scoped clarification, never an answer to a broad question."""
    if status != "waiting_for_user":
        return
    # Intentionally conservative: we do not auto-answer. Hook left for extension.
    return


async def run_monitor(cfg: Config, stop: asyncio.Event) -> None:
    devin = DevinClient(cfg)
    gh = GitHubClient(cfg)
    backoff = MIN_BACKOFF
    try:
        while not stop.is_set():
            worked = False
            try:
                jobs = await asyncio.to_thread(db.jobs_needing_poll)
                for job in jobs:
                    if job["state"] != states.RUNNING or not job.get("session_id"):
                        continue
                    worked = True
                    try:
                        session = await devin.get_session(job["session_id"])
                    except DevinError as exc:
                        if exc.status_code == 429:
                            backoff = min(MAX_BACKOFF, max(backoff, exc.retry_after or backoff))
                            event(log, "monitor_rate_limited", retry_after=backoff)
                            break
                        event(log, "monitor_get_failed", job_id=job["id"], error=str(exc))
                        continue
                    await _evaluate(cfg, gh, job, session)
            except Exception:  # noqa: BLE001
                log.exception("monitor loop iteration failed")

            backoff = MIN_BACKOFF if worked else min(MAX_BACKOFF, backoff * 1.5)
            await _sleep_or_stop(stop, backoff)
    finally:
        await devin.aclose()
        await gh.aclose()


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
