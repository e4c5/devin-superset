"""Session worker: turns a reserved job into a governed Devin session.

Runs outside the webhook request path. Persists `creating` + correlation tag
before the outbound POST; on an uncertain POST it never blind-retries — it
reconciles by listing org sessions filtered by the correlation tag.
"""
from __future__ import annotations

import asyncio
import logging

from . import db, states
from .config import Config
from .devin import DevinClient, DevinError, build_create_payload
from .github import GitHubClient
from .logging_utils import event

log = logging.getLogger("ops_guard.worker")

POLL_INTERVAL = 3.0


async def _process(cfg: Config, devin: DevinClient, gh: GitHubClient, job: dict) -> None:
    job_id = job["id"]
    payload = build_create_payload(cfg, job, job.get("issue_body") or "")

    try:
        resp = await devin.create_session(payload)
    except DevinError as exc:
        # 4xx (bad request / auth / quota) is deterministic: the session was not
        # created. Anything else is uncertain -> reconcile, do not retry.
        if exc.status_code and 400 <= exc.status_code < 500 and exc.status_code != 429:
            db.finalize(job_id, state=states.FAILED, error=f"create rejected: {exc}")
            event(log, "session_create_failed", job_id=job_id, error=str(exc))
            await _safe_comment(gh, job, f"Ops Guard could not start a Devin session: `{exc}`")
            return
        db.mark_creation_unknown(job_id, error=str(exc))
        event(log, "session_create_unknown", job_id=job_id, error=str(exc))
        return
    except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
        db.mark_creation_unknown(job_id, error=repr(exc))
        event(log, "session_create_unknown", job_id=job_id, error=repr(exc))
        return

    session_id = resp.get("session_id") or resp.get("id") or ""
    session_url = resp.get("url") or resp.get("session_url") or ""
    status = resp.get("status") or "running"
    if not session_id:
        db.mark_creation_unknown(job_id, error=f"create response missing session_id: {resp}")
        return

    db.mark_running(job_id, session_id=session_id, session_url=session_url, status=status)
    event(log, "session_created", job_id=job_id, session_id=session_id)
    await _safe_comment(
        gh, job,
        f"Devin session started for this issue.\n\nSession: {session_url}\n"
        f"ACU limit: {cfg.max_acu_limit}",
    )


async def _reconcile(cfg: Config, devin: DevinClient, job: dict) -> None:
    job_id = job["id"]
    try:
        sessions = await devin.list_sessions_by_tag(job["correlation_tag"])
    except DevinError as exc:
        event(log, "reconcile_failed", job_id=job_id, error=str(exc))
        return

    if len(sessions) == 1:
        s = sessions[0]
        sid = s.get("session_id") or s.get("id") or ""
        if sid:
            db.attach_reconciled_session(
                job_id,
                session_id=sid,
                session_url=s.get("url") or s.get("session_url") or "",
                status=s.get("status") or "running",
            )
            event(log, "reconciled", job_id=job_id, session_id=sid)
            return
    if len(sessions) == 0:
        # Devin never accepted it: safe to requeue for one more attempt is risky
        # (tag already consumed). Leave for human review with a clear signal.
        db.finalize(job_id, state=states.NEEDS_REVIEW,
                    error="creation_unknown: no session found for correlation tag")
        event(log, "reconcile_no_session", job_id=job_id)
        return
    db.finalize(job_id, state=states.NEEDS_REVIEW,
                error=f"creation_unknown: {len(sessions)} sessions share the correlation tag")
    event(log, "reconcile_ambiguous", job_id=job_id, count=len(sessions))


async def _safe_comment(gh: GitHubClient, job: dict, body: str) -> None:
    try:
        await gh.comment_on_issue(job["issue_number"], body)
    except Exception as exc:  # noqa: BLE001
        log.warning("issue comment failed for #%s: %s", job["issue_number"], exc)


async def run_worker(cfg: Config, stop: asyncio.Event) -> None:
    devin = DevinClient(cfg)
    gh = GitHubClient(cfg)
    try:
        while not stop.is_set():
            try:
                job = await asyncio.to_thread(db.claim_queued_job)
                if job is not None:
                    await _process(cfg, devin, gh, job)
                    continue

                for unknown in await asyncio.to_thread(_unknown_jobs):
                    await _reconcile(cfg, devin, unknown)
            except Exception:  # noqa: BLE001
                log.exception("worker loop iteration failed")
            await _sleep_or_stop(stop, POLL_INTERVAL)
    finally:
        await devin.aclose()
        await gh.aclose()


def _unknown_jobs() -> list[dict]:
    return [j for j in db.jobs_needing_poll() if j["state"] == states.CREATION_UNKNOWN]


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
