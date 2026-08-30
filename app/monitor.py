"""Session monitor: polls each non-terminal session and verifies completion.

`status: exit` means the session ended, NOT that a fix was delivered. The
delivered signal is a pull request: a run is `remediated` once GitHub confirms
a PR that Devin opened for the issue — targets the fork's default branch,
references the issue, and is open or merged — regardless of session status.
A session that exits without such a PR is `needs_review` / `failed`. A session
paused on a human is kept under observation for `WAITING_GRACE` (it may still
open a PR) before it is routed to `needs_review`.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Optional

from . import db, states
from .config import Config
from .devin import DevinClient, DevinError
from .github import GitHubClient
from .logging_utils import event

log = logging.getLogger("ops_guard.monitor")

MIN_BACKOFF = 15.0
MAX_BACKOFF = 60.0

# A session paused on a human (`waiting_for_user` / `waiting_for_approval`) with
# no PR yet is not abandoned immediately: Devin may still open one, and a PR is
# the completion signal. Keep polling until the session opens a PR, ends, or has
# been waiting this long — only then route it to `needs_review`.
WAITING_GRACE = 1800.0

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

# `waiting` records when the session first paused on a human — the start of the
# grace window; `waiting_notice` records that the issue comment was delivered.
_WAITING_EVENT = "waiting"
_WAITING_NOTICE_EVENT = "waiting_notice"


def _extract_pr_url(session: dict[str, Any],
                    structured: Optional[dict]) -> tuple[Optional[str], bool]:
    """Return (pr_url, attributed).

    `attributed` is True only when the Devin API itself lists the PR on the
    session, i.e. Devin opened it. A URL that appears only in the session's
    self-reported `structured_output` is unattributed: a session could name any
    pre-existing PR, so the caller must establish the link separately.
    """
    prs = session.get("pull_requests") or session.get("pull_request") or []
    if isinstance(prs, dict):
        prs = [prs]
    for pr in prs:
        url = pr.get("pr_url") or pr.get("url") or pr.get("html_url")
        if url:
            return url, True
    if structured and structured.get("pr_url"):
        return structured["pr_url"], False
    return None, False


def _references_issue(body: str, repo: str, issue_number: int) -> bool:
    """True only for a reference to `repo`'s issue #n.

    A bare `#n` counts, but a cross-repository `other/repo#n` (or an issue URL
    under another repository) must not: those point at somebody else's issue
    that merely shares a number.
    """
    n = issue_number
    patterns = (
        rf"(?<![\w./#-])#{n}\b",
        rf"(?<![\w./-]){re.escape(repo)}#{n}\b",
        rf"github\.com/{re.escape(repo)}/issues/{n}\b",
    )
    return any(re.search(p, body, re.IGNORECASE) for p in patterns)


def _pr_is_devin_delivery(body: str, job: dict) -> bool:
    """True when the PR body ties the PR back to this job's Devin session.

    Devin appends the session link to the PRs it opens, so the session id in
    the body is evidence the PR came from this run.
    """
    body = body.lower()
    session_id = (job.get("session_id") or "").strip().lower()
    session_url = (job.get("session_url") or "").strip().lower()
    return bool((session_id and session_id in body) or (session_url and session_url in body))


async def _verify_pr_open(cfg: Config, gh: GitHubClient, pr_url: Optional[str],
                          job: dict, attributed: bool) -> bool:
    """A PR only counts if Devin opened it for this job, it targets
    TARGET_REPOSITORY's default branch, and it is open or merged."""
    if not pr_url:
        return False
    issue_number = job["issue_number"]
    try:
        status = await gh.pr_status(pr_url)
    except Exception as exc:  # noqa: BLE001
        log.warning("PR verification failed for %s: %s", pr_url, exc)
        return False
    if not status:
        return False

    # Fail closed: every check must be affirmatively satisfied. Missing metadata
    # (no base repo, no base ref, default-branch lookup failed) is a rejection,
    # not a pass.
    target = cfg.target_repository.lower()
    if status["url_repo"].lower() != target or (status.get("base_repo") or "").lower() != target:
        log.warning("PR %s repo %s / base %s != %s — rejecting",
                    pr_url, status["url_repo"], status.get("base_repo"), cfg.target_repository)
        return False

    # Must reference the triggering issue. A GitHub closing keyword
    # ("Fixes #123") is ideal, but Devin follows the repo's PR template, which
    # links the issue without a closing keyword ("Has associated issue: #123"),
    # so a bare "#123" mention in the body is accepted too.
    body = status.get("body") or ""
    if not _references_issue(body, cfg.target_repository, issue_number):
        log.warning("PR %s body does not reference issue #%s — rejecting",
                    pr_url, issue_number)
        return False

    # A PR the Devin API does not list on the session is only self-reported;
    # accept it solely when its body links back to this job's session.
    if not attributed and not _pr_is_devin_delivery(body, job):
        log.warning("PR %s is not attributable to session %s — rejecting",
                    pr_url, job.get("session_id"))
        return False

    # Must target the fork's default branch, not some side branch.
    default = await gh.default_branch(cfg.target_repository)
    if not default or not status.get("base_ref") or status["base_ref"] != default:
        log.warning("PR %s base ref %r != default %r (or lookup failed) — rejecting",
                    pr_url, status.get("base_ref"), default)
        return False

    return status["state"] == "open" or status["merged"]


async def _evaluate(cfg: Config, gh: GitHubClient, job: dict, session: dict[str, Any]) -> None:
    job_id = job["id"]
    status = (session.get("status") or "").lower()
    detail = (session.get("status_detail") or "").lower()
    acus = session.get("acus_consumed")
    structured = session.get("structured_output")
    if isinstance(structured, str):
        structured = None
    pr_url, pr_attributed = _extract_pr_url(
        session, structured if isinstance(structured, dict) else None)

    db.record_poll(
        job_id,
        status=status or None,
        status_detail=detail or None,
        acus_consumed=float(acus) if isinstance(acus, (int, float)) else None,
        pr_url=pr_url,
        structured_output=structured,
    )

    outcome = structured.get("outcome") if isinstance(structured, dict) else None

    # The success bar is "Devin opened a PR for this issue." As soon as GitHub
    # shows a PR that targets the fork's default branch and references the
    # issue, the job is `remediated` — even if the session is still running or
    # paused waiting for a human.
    if await _verify_pr_open(cfg, gh, pr_url, job, pr_attributed):
        tests = ", ".join((structured or {}).get("tests_run") or []) or "n/a"
        await _finish(
            gh, job, states.REMEDIATED, status or "running",
            f"Remediated. PR: {pr_url}\nTests run: {tests}\n"
            f"ACUs: {acus if acus is not None else 'n/a'}",
            pr_url=pr_url,
        )
        return

    # Errors / billing / usage limits -> failed.
    if status == "error" or detail == "error":
        await _finish(gh, job, states.FAILED, status,
                      f"Devin session errored ({detail or status}).")
        return
    if status == "suspended" or detail in _BILLING_DETAILS:
        await _finish(gh, job, states.FAILED, status,
                      f"Devin session suspended ({detail or status}). Session: {job.get('session_url')}")
        return

    # Paused waiting on a human (session `status` stays `running`), no PR yet.
    # Keep polling within the grace window — a PR would flip this to
    # `remediated` on the next pass. Post the "needs input" note once.
    if detail in _WAITING_DETAILS:
        await _maybe_nudge(cfg, job, session, detail)
        waiting_since = await asyncio.to_thread(
            db.first_seen_at, job_id, _WAITING_EVENT, detail
        )
        await _notify_waiting(gh, job, detail)
        if time.time() - waiting_since < WAITING_GRACE:
            return
        await _finish(gh, job, states.NEEDS_REVIEW, status,
                      f"Devin has been {detail.replace('_', ' ')} for over "
                      f"{int(WAITING_GRACE // 60)} min without opening a PR — needs a human. "
                      f"Session: {job.get('session_url')}")
        return

    # Still working -> keep polling.
    if status in _STATUS_WORKING or status == "":
        return

    # status == "exit" and no verifiable PR: the session ended without
    # delivering. Ended != succeeded.
    if outcome in ("blocked", "not_reproducible"):
        blocker = (structured or {}).get("blocker") or outcome
        await _finish(gh, job, states.NEEDS_REVIEW, status,
                      f"Devin reported `{outcome}`: {blocker}\nSession: {job.get('session_url')}")
        return
    await _finish(
        gh, job, states.NEEDS_REVIEW, status,
        f"Session ended ({detail or 'no detail'}) without a PR that GitHub can verify.\n"
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


async def _notify_waiting(gh: GitHubClient, job: dict, detail: str) -> None:
    """Tell the issue once that the session needs input, retrying on failure.

    The delivery marker is persisted only after GitHub accepts the comment, so
    a transient failure is retried on the next poll. The comment carries a
    hidden marker so a retry after a crash between the accepted request and the
    local write finds the existing comment instead of posting a second one.
    """
    job_id = job["id"]
    issue_number = job["issue_number"]
    if await asyncio.to_thread(db.has_event, job_id, _WAITING_NOTICE_EVENT):
        return
    marker = f"<!-- ops-guard:waiting:{job_id} -->"
    try:
        if not await gh.issue_has_comment(issue_number, marker):
            await gh.comment_on_issue(
                issue_number,
                f"{marker}\n**Ops Guard** — Devin is {detail.replace('_', ' ')} and may need "
                f"input. Session: {job.get('session_url')}\nStill watching for a PR.",
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("waiting note failed for #%s: %s", issue_number, exc)
        return
    await asyncio.to_thread(db.add_event_once, job_id, _WAITING_NOTICE_EVENT, detail)


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
