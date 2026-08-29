#!/usr/bin/env python3
"""Offline self-test: webhook auth, dedup, one-active-job, state machine.

No network calls to Devin/GitHub — the worker/monitor loops are not started
(TestClient does not run lifespan unless entered as a context manager).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DEVIN_SERVICE_USER_TOKEN", "test-token")
os.environ.setdefault("DEVIN_ORG_ID", "org-test")
os.environ.setdefault("DEVIN_REPOSITORY_ID", "repo-test")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "shhh")
os.environ.setdefault("GITHUB_TOKEN", "gh-test")
os.environ.setdefault("TARGET_REPOSITORY", "acme/superset")
os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import db, states  # noqa: E402
from app.main import app  # noqa: E402

SECRET = "shhh"
PASS = 0
FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def sign(raw: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def payload(issue: int, action: str = "labeled", repo: str = "acme/superset") -> bytes:
    body = {
        "action": action,
        "issue": {
            "number": issue,
            "title": f"issue {issue}",
            "body": "test: pytest x",
            "html_url": f"https://github.com/{repo}/issues/{issue}",
            "labels": [{"name": "devin-autofix"}] if action == "opened" else [],
        },
        "repository": {"full_name": repo, "id": 1},
    }
    if action == "labeled":
        body["label"] = {"name": "devin-autofix"}
    return json.dumps(body).encode()


def post(client, raw, *, event="issues", delivery=None, sig=None):
    return client.post(
        "/webhook/github",
        content=raw,
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery or str(uuid.uuid4()),
            "X-Hub-Signature-256": sig or sign(raw),
            "Content-Type": "application/json",
        },
    )


def main() -> int:
    db.init(os.environ["DATABASE_PATH"])
    client = TestClient(app, raise_server_exceptions=True)

    raw = payload(1)
    r = post(client, raw, sig="sha256=deadbeef")
    check("bad signature -> 401", r.status_code == 401)

    r = post(client, payload(2, repo="evil/fork"))
    check("wrong repo -> 403", r.status_code == 403)

    r = post(client, payload(3, action="edited"))
    check("non-trigger action -> 200 ignored", r.status_code == 200 and r.json()["status"] == "ignored")

    r = post(client, raw, event="push")
    check("non-issues event -> 200 ignored", r.status_code == 200)

    d = str(uuid.uuid4())
    r1 = post(client, payload(10), delivery=d)
    r2 = post(client, payload(10), delivery=d)
    check("first delivery -> 202 accepted", r1.status_code == 202)
    check("same delivery id -> 200 duplicate", r2.status_code == 200 and r2.json()["status"] == "duplicate_delivery")

    r3 = post(client, payload(10))  # different delivery, same issue
    check("second delivery same issue -> 200 already_active", r3.status_code == 200 and r3.json()["status"] == "already_active")

    jobs = [j for j in db.list_jobs() if j["issue_number"] == 10]
    check("exactly one job row for issue 10", len(jobs) == 1)
    check("job in queued state", jobs[0]["state"] == states.QUEUED)
    check("correlation tag set", jobs[0]["correlation_tag"] == "ops-guard:issue:10:attempt:1")

    # simulate worker claiming + completing, then a new delivery is allowed
    claimed = db.claim_queued_job()
    check("claim moves queued -> creating", claimed and claimed["state"] == states.CREATING)
    db.mark_running(claimed["id"], session_id="sess-1", session_url="u", status="running")
    db.finalize(claimed["id"], state=states.REMEDIATED, status="exit", pr_url="https://github.com/acme/superset/pull/5")

    r4 = post(client, payload(10))
    check("new delivery after terminal -> 202 (attempt 2)", r4.status_code == 202)
    jobs = [j for j in db.list_jobs() if j["issue_number"] == 10]
    check("now two job rows for issue 10", len(jobs) == 2)
    check("attempt 2 tag", any(j["correlation_tag"] == "ops-guard:issue:10:attempt:2" for j in jobs))

    # observability
    check("/health ok", client.get("/health").json()["status"] == "ok")
    m = client.get("/metrics").json()
    check("/metrics remediated_total == 1", m["remediated_total"] == 1)
    check("/metrics prs_opened_total == 1", m["prs_opened_total"] == 1)
    check("/runs/10 returns audit record with events", "events" in client.get("/runs/10").json())
    check("/runs/999 -> 404", client.get("/runs/999").status_code == 404)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
