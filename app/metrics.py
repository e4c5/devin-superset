"""Metrics computed locally from job records."""
from __future__ import annotations

from statistics import median
from typing import Any

from . import db, states


def compute() -> dict[str, Any]:
    jobs = db.list_jobs()

    def count(state: str) -> int:
        return sum(1 for j in jobs if j["state"] == state)

    active = sum(1 for j in jobs if j["state"] in states.ACTIVE_STATES)
    remediated = [j for j in jobs if j["state"] == states.REMEDIATED]

    elapsed = [
        j["completed_at"] - j["created_at"]
        for j in jobs
        if j.get("completed_at") and j.get("created_at")
    ]
    acu_values = [
        j["acus_consumed"] for j in remediated if isinstance(j.get("acus_consumed"), (int, float))
    ]
    prs_opened = sum(1 for j in jobs if j.get("pr_url"))

    return {
        "active_runs": active,
        "remediated_total": len(remediated),
        "needs_review_total": count(states.NEEDS_REVIEW),
        "failed_total": count(states.FAILED),
        "reconciled_total": db.reconciled_count(),
        "prs_opened_total": prs_opened,
        "median_elapsed_seconds": round(median(elapsed), 1) if elapsed else None,
        "acus_per_remediated_run": round(sum(acu_values) / len(acu_values), 2) if acu_values else None,
        "total_jobs": len(jobs),
    }
