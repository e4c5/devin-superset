"""SQLite persistence with WAL mode and BEGIN IMMEDIATE write transactions.

All functions here are synchronous and short-lived. Async callers should invoke
them via ``asyncio.to_thread``. A module-level lock serializes writers within
this process; WAL + BEGIN IMMEDIATE guards against other processes/restarts.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from . import states

_write_lock = threading.Lock()
_db_path: str = ""


SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id   TEXT PRIMARY KEY,
    received_at   REAL NOT NULL,
    repository_id TEXT NOT NULL,
    event         TEXT NOT NULL,
    action        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_id         TEXT NOT NULL,
    issue_number          INTEGER NOT NULL,
    issue_url             TEXT NOT NULL,
    title                 TEXT NOT NULL,
    issue_body            TEXT NOT NULL DEFAULT '',
    state                 TEXT NOT NULL,
    attempt               INTEGER NOT NULL DEFAULT 1,
    correlation_tag       TEXT UNIQUE NOT NULL,
    session_id            TEXT UNIQUE,
    session_url           TEXT,
    created_at            REAL NOT NULL,
    started_at            REAL,
    completed_at          REAL,
    last_polled_at        REAL,
    status                TEXT,
    status_detail         TEXT,
    acus_consumed         REAL,
    pr_url                TEXT,
    structured_output_json TEXT,
    error                 TEXT
);

-- One active job per (repository_id, issue_number).
CREATE UNIQUE INDEX IF NOT EXISTS one_active_job_per_issue
    ON jobs (repository_id, issue_number)
    WHERE state IN ('queued', 'creating', 'running', 'creation_unknown');

CREATE TABLE IF NOT EXISTS job_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id    INTEGER NOT NULL,
    at        REAL NOT NULL,
    kind      TEXT NOT NULL,
    detail    TEXT
);
"""


def init(db_path: str) -> None:
    global _db_path
    _db_path = db_path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def _writer() -> Iterator[sqlite3.Connection]:
    with _write_lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    d = dict(row)
    if d.get("structured_output_json"):
        try:
            d["structured_output"] = json.loads(d["structured_output_json"])
        except (TypeError, ValueError):
            d["structured_output"] = None
    else:
        d["structured_output"] = None
    return d


# --------------------------------------------------------------------------- #
# Reservation: dedupe delivery + reserve one active job, in a single txn.
# --------------------------------------------------------------------------- #

class ReserveResult:
    RESERVED = "reserved"
    DUPLICATE_DELIVERY = "duplicate_delivery"
    ALREADY_ACTIVE = "already_active"

    def __init__(self, outcome: str, job: Optional[dict[str, Any]] = None):
        self.outcome = outcome
        self.job = job


def reserve_job(
    *,
    delivery_id: str,
    repository_id: str,
    event: str,
    action: str,
    issue_number: int,
    issue_url: str,
    title: str,
    issue_body: str = "",
) -> ReserveResult:
    now = time.time()
    with _writer() as conn:
        existing = conn.execute(
            "SELECT 1 FROM deliveries WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()
        if existing is not None:
            return ReserveResult(ReserveResult.DUPLICATE_DELIVERY)

        conn.execute(
            "INSERT INTO deliveries (delivery_id, received_at, repository_id, event, action) "
            "VALUES (?, ?, ?, ?, ?)",
            (delivery_id, now, repository_id, event, action),
        )

        active = conn.execute(
            f"SELECT * FROM jobs WHERE repository_id = ? AND issue_number = ? "
            f"AND state IN ({','.join('?' for _ in states.ACTIVE_STATES)})",
            (repository_id, issue_number, *states.ACTIVE_STATES),
        ).fetchone()
        if active is not None:
            return ReserveResult(ReserveResult.ALREADY_ACTIVE, _row_to_dict(active))

        attempt_row = conn.execute(
            "SELECT COALESCE(MAX(attempt), 0) + 1 AS n FROM jobs "
            "WHERE repository_id = ? AND issue_number = ?",
            (repository_id, issue_number),
        ).fetchone()
        attempt = int(attempt_row["n"])
        correlation_tag = f"ops-guard:issue:{issue_number}:attempt:{attempt}"

        cur = conn.execute(
            "INSERT INTO jobs (repository_id, issue_number, issue_url, title, issue_body, "
            "state, attempt, correlation_tag, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (repository_id, issue_number, issue_url, title, issue_body, states.QUEUED,
             attempt, correlation_tag, now),
        )
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (cur.lastrowid,)).fetchone()
        conn.execute(
            "INSERT INTO job_events (job_id, at, kind, detail) VALUES (?, ?, ?, ?)",
            (cur.lastrowid, now, "reserved", f"delivery={delivery_id}"),
        )
        return ReserveResult(ReserveResult.RESERVED, _row_to_dict(job))


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

def get_job(job_id: int) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        return _row_to_dict(
            conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        )


def get_job_by_issue(repository_id: str, issue_number: int) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        return _row_to_dict(
            conn.execute(
                "SELECT * FROM jobs WHERE repository_id = ? AND issue_number = ? "
                "ORDER BY attempt DESC LIMIT 1",
                (repository_id, issue_number),
            ).fetchone()
        )


def list_jobs() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchall()
        return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


def job_events(job_id: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT at, kind, detail FROM job_events WHERE job_id = ? ORDER BY id",
            (job_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def claim_queued_job() -> Optional[dict[str, Any]]:
    """Atomically move one queued job to `creating` and return it."""
    now = time.time()
    with _writer() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE state = ? ORDER BY id LIMIT 1", (states.QUEUED,)
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE jobs SET state = ?, started_at = ? WHERE id = ?",
            (states.CREATING, now, row["id"]),
        )
        conn.execute(
            "INSERT INTO job_events (job_id, at, kind, detail) VALUES (?, ?, ?, ?)",
            (row["id"], now, "state", f"{states.QUEUED} -> {states.CREATING}"),
        )
        return _row_to_dict(
            conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
        )


def reconciled_count() -> int:
    """How many jobs were recovered by correlation-tag reconciliation."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT job_id) AS n FROM job_events WHERE kind = 'reconciled'"
        ).fetchone()
        return int(row["n"])


def jobs_needing_poll() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE state IN "
            f"({','.join('?' for _ in states.NON_TERMINAL_STATES)}) ORDER BY id",
            tuple(states.NON_TERMINAL_STATES),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #

def _log_event(conn: sqlite3.Connection, job_id: int, kind: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO job_events (job_id, at, kind, detail) VALUES (?, ?, ?, ?)",
        (job_id, time.time(), kind, detail),
    )


def mark_running(job_id: int, *, session_id: str, session_url: str, status: str) -> None:
    with _writer() as conn:
        conn.execute(
            "UPDATE jobs SET state = ?, session_id = ?, session_url = ?, status = ?, "
            "last_polled_at = ? WHERE id = ?",
            (states.RUNNING, session_id, session_url, status, time.time(), job_id),
        )
        _log_event(conn, job_id, "state", f"-> {states.RUNNING} session={session_id}")


def mark_creation_unknown(job_id: int, *, error: str) -> None:
    with _writer() as conn:
        conn.execute(
            "UPDATE jobs SET state = ?, error = ? WHERE id = ?",
            (states.CREATION_UNKNOWN, error, job_id),
        )
        _log_event(conn, job_id, "state", f"-> {states.CREATION_UNKNOWN}: {error}")


def attach_reconciled_session(
    job_id: int, *, session_id: str, session_url: str, status: str
) -> None:
    with _writer() as conn:
        conn.execute(
            "UPDATE jobs SET state = ?, session_id = ?, session_url = ?, status = ?, "
            "error = NULL WHERE id = ?",
            (states.RUNNING, session_id, session_url, status, job_id),
        )
        _log_event(conn, job_id, "reconciled", f"session={session_id}")


def record_poll(
    job_id: int,
    *,
    status: Optional[str],
    status_detail: Optional[str],
    acus_consumed: Optional[float],
    pr_url: Optional[str],
    structured_output: Any,
) -> None:
    with _writer() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, status_detail = ?, acus_consumed = ?, "
            "pr_url = COALESCE(?, pr_url), structured_output_json = ?, "
            "last_polled_at = ? WHERE id = ?",
            (
                status,
                status_detail,
                acus_consumed,
                pr_url,
                json.dumps(structured_output) if structured_output is not None else None,
                time.time(),
                job_id,
            ),
        )


def finalize(
    job_id: int,
    *,
    state: str,
    status: Optional[str] = None,
    pr_url: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    with _writer() as conn:
        conn.execute(
            "UPDATE jobs SET state = ?, status = COALESCE(?, status), "
            "pr_url = COALESCE(?, pr_url), error = COALESCE(?, error), "
            "completed_at = ? WHERE id = ?",
            (state, status, pr_url, error, time.time(), job_id),
        )
        _log_event(conn, job_id, "state", f"-> {state}")
