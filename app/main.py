"""FastAPI application: webhook gateway + observability endpoints.

Background worker and monitor tasks run in the same process (single Docker
Compose service, SQLite on a mounted volume).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse

from . import db, metrics, states
from .config import ConfigError, get_config
from .logging_utils import configure as configure_logging
from .logging_utils import event
from .monitor import run_monitor
from .webhook import WebhookDecision, evaluate, verify_signature
from .worker import run_worker

log = logging.getLogger("ops_guard.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    try:
        cfg = get_config()
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        raise

    db.init(cfg.database_path)
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(run_worker(cfg, stop), name="worker"),
        asyncio.create_task(run_monitor(cfg, stop), name="monitor"),
    ]
    event(log, "startup", target_repo=cfg.target_repository, org=cfg.devin_org_id)
    try:
        yield
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        event(log, "shutdown")


app = FastAPI(title="Devin-Ops Guard", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    try:
        await asyncio.to_thread(db.list_jobs)
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "db": db_ok}


@app.get("/metrics")
async def metrics_endpoint() -> dict:
    return await asyncio.to_thread(metrics.compute)


@app.get("/runs")
async def runs() -> dict:
    jobs = await asyncio.to_thread(db.list_jobs)
    return {
        "runs": [
            {
                "issue_number": j["issue_number"],
                "title": j["title"],
                "state": j["state"],
                "status": j.get("status"),
                "session_url": j.get("session_url"),
                "pr_url": j.get("pr_url"),
                "acus_consumed": j.get("acus_consumed"),
                "created_at": j.get("created_at"),
                "completed_at": j.get("completed_at"),
            }
            for j in jobs
        ]
    }


@app.get("/runs/{issue_number}")
async def run_detail(issue_number: int) -> Response:
    cfg = get_config()
    job = await asyncio.to_thread(db.get_job_by_issue, _repo_id(cfg), issue_number)
    if job is None:
        # fall back: match by issue number alone across repos
        for j in await asyncio.to_thread(db.list_jobs):
            if j["issue_number"] == issue_number:
                job = j
                break
    if job is None:
        return JSONResponse({"error": "no run for that issue"}, status_code=404)
    events = await asyncio.to_thread(db.job_events, job["id"])
    job["events"] = events
    job.pop("issue_body", None)
    return JSONResponse(job)


@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
    x_hub_signature_256: str = Header(default=""),
) -> Response:
    cfg = get_config()
    raw = await request.body()

    if not verify_signature(cfg.github_webhook_secret, raw, x_hub_signature_256):
        event(log, "webhook_rejected", reason="bad_signature", delivery=x_github_delivery)
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid json"}, status_code=400)

    decision = evaluate(cfg, x_github_event, payload)

    if decision.kind == WebhookDecision.REJECT_REPO:
        event(log, "webhook_rejected", reason=decision.reason, delivery=x_github_delivery)
        return JSONResponse({"error": decision.reason}, status_code=403)

    if decision.kind == WebhookDecision.IGNORE:
        return JSONResponse({"status": "ignored", "reason": decision.reason}, status_code=200)

    ev = decision.event
    assert ev is not None
    result = await asyncio.to_thread(
        db.reserve_job,
        delivery_id=x_github_delivery or f"synthetic:{ev.repository_id}:{ev.issue_number}",
        repository_id=ev.repository_id,
        event=x_github_event,
        action=ev.action,
        issue_number=ev.issue_number,
        issue_url=ev.issue_url,
        title=ev.title,
        issue_body=(payload.get("issue") or {}).get("body") or "",
    )

    if result.outcome == db.ReserveResult.DUPLICATE_DELIVERY:
        event(log, "webhook_duplicate", delivery=x_github_delivery)
        return JSONResponse({"status": "duplicate_delivery"}, status_code=200)

    if result.outcome == db.ReserveResult.ALREADY_ACTIVE:
        event(log, "webhook_already_active", issue=ev.issue_number,
              job_state=result.job["state"] if result.job else None)
        return JSONResponse(
            {"status": "already_active", "issue_number": ev.issue_number}, status_code=200
        )

    event(log, "job_reserved", issue=ev.issue_number, job_id=result.job["id"])
    return JSONResponse(
        {"status": "accepted", "issue_number": ev.issue_number, "job_id": result.job["id"]},
        status_code=202,
    )


def _repo_id(cfg) -> str:
    return cfg.target_repository
