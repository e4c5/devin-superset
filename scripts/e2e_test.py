#!/usr/bin/env python3
"""End-to-end worker+monitor test with mocked Devin and GitHub HTTP.

Proves: session create -> running -> monitor verifies completion, and that
`exit` without a verifiable open PR does NOT become `remediated`.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.update(
    DEVIN_SERVICE_USER_TOKEN="t", DEVIN_ORG_ID="org-test", DEVIN_REPOSITORY_ID="r",
    GITHUB_WEBHOOK_SECRET="s", GITHUB_TOKEN="g", TARGET_REPOSITORY="acme/superset",
    DATABASE_PATH=os.path.join(tempfile.mkdtemp(), "e2e.db"),
)

import httpx  # noqa: E402

from app import db, states  # noqa: E402
from app.config import get_config  # noqa: E402
from app import devin as devin_mod, github as gh_mod  # noqa: E402
from app.worker import _process  # noqa: E402
from app.monitor import _evaluate  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    ok = bool(cond)
    print(("  ok   " if ok else "  FAIL ") + name)
    PASS += ok
    FAIL += (not ok)


def devin_client(session_state, sid="sess-9"):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/sessions"):
            return httpx.Response(200, json={"session_id": sid, "url": f"https://app.devin.ai/sessions/{sid}", "status": "running"})
        if req.method == "GET" and f"/sessions/{sid}" in req.url.path:
            return httpx.Response(200, json=session_state)
        return httpx.Response(404, json={})
    cfg = get_config()
    return devin_mod.DevinClient(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def gh_client(pr_state, issue_no=1, base_ref="main"):
    def handler(req: httpx.Request) -> httpx.Response:
        parts = req.url.path.strip("/").split("/")
        if req.method == "POST" and req.url.path.endswith("/comments"):
            return httpx.Response(201, json={"id": 1})
        if req.method == "GET" and "/pulls/" in req.url.path:
            if pr_state is None:
                return httpx.Response(404, json={})
            # /repos/{owner}/{repo}/pulls/{n} -> base repo echoes the URL repo
            base_repo = f"{parts[1]}/{parts[2]}"
            body = {"base": {"repo": {"full_name": base_repo}, "ref": base_ref},
                    "body": pr_state.pop("body", f"Fixes #{issue_no}"), **pr_state}
            return httpx.Response(200, json=body)
        if req.method == "GET" and len(parts) == 3 and parts[0] == "repos":
            return httpx.Response(200, json={"default_branch": "main"})
        return httpx.Response(404, json={})
    cfg = get_config()
    return gh_mod.GitHubClient(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def scenario(name, session_final, pr_state, expected_state, base_ref="main"):
    issue_no = hash(name) % 1000
    db.reserve_job(delivery_id=f"d-{name}", repository_id="acme/superset", event="issues",
                   action="labeled", issue_number=issue_no, issue_url="https://github.com/acme/superset/issues/1",
                   title="t", issue_body="do the thing")
    job = db.claim_queued_job()
    cfg = get_config()
    sid = f"sess-{name}"
    dc = devin_client({"status": "running"}, sid=sid)
    gc = gh_client(pr_state, issue_no=issue_no, base_ref=base_ref)
    await _process(cfg, dc, gc, job)
    j = db.get_job(job["id"])
    check(f"[{name}] job running after create", j["state"] == states.RUNNING and j["session_id"] == sid)
    await _evaluate(cfg, gc, j, session_final)
    j = db.get_job(job["id"])
    check(f"[{name}] final state == {expected_state} (got {j['state']})", j["state"] == expected_state)
    await dc.aclose()
    await gc.aclose()


async def main() -> int:
    db.init(os.environ["DATABASE_PATH"])

    await scenario(
        "happy",
        {"status": "exit", "acus_consumed": 4.5,
         "structured_output": {"outcome": "remediated", "summary": "ok", "tests_run": ["pytest x"],
                               "pr_url": "https://github.com/acme/superset/pull/7", "blocker": None},
         "pull_requests": [{"url": "https://github.com/acme/superset/pull/7"}]},
        {"state": "open", "merged": False},
        states.REMEDIATED,
    )

    await scenario(
        "exit-but-pr-missing",
        {"status": "exit",
         "structured_output": {"outcome": "remediated", "summary": "claims done", "tests_run": [],
                               "pr_url": "https://github.com/acme/superset/pull/8", "blocker": None}},
        None,  # GitHub says PR does not exist
        states.NEEDS_REVIEW,
    )

    await scenario(
        "blocked",
        {"status": "exit",
         "structured_output": {"outcome": "blocked", "summary": "x", "tests_run": [],
                               "pr_url": None, "blocker": "cannot reproduce locally"}},
        None,
        states.NEEDS_REVIEW,
    )

    await scenario(
        "errored",
        {"status": "error", "status_detail": "acu limit reached"},
        None,
        states.FAILED,
    )

    await scenario(
        "waiting",
        {"status": "running", "status_detail": "waiting_for_user"},
        None,
        states.NEEDS_REVIEW,
    )

    await scenario(
        "suspended-billing",
        {"status": "suspended", "status_detail": "out_of_credits"},
        None,
        states.FAILED,
    )

    # PR points at a DIFFERENT repo than TARGET_REPOSITORY -> not remediated.
    await scenario(
        "pr-wrong-repo",
        {"status": "exit", "acus_consumed": 3.0,
         "structured_output": {"outcome": "remediated", "summary": "ok", "tests_run": ["pytest x"],
                               "pr_url": "https://github.com/attacker/superset/pull/1", "blocker": None}},
        {"state": "open", "merged": False},
        states.NEEDS_REVIEW,
    )

    # PR body does not reference the triggering issue -> not remediated.
    await scenario(
        "pr-no-issue-ref",
        {"status": "exit", "acus_consumed": 3.0,
         "structured_output": {"outcome": "remediated", "summary": "ok", "tests_run": ["pytest x"],
                               "pr_url": "https://github.com/acme/superset/pull/9", "blocker": None}},
        {"state": "open", "merged": False, "body": "a fix, no reference"},
        states.NEEDS_REVIEW,
    )

    # PR targets a side branch, not the fork default -> not remediated.
    await scenario(
        "pr-wrong-branch",
        {"status": "exit", "acus_consumed": 3.0,
         "structured_output": {"outcome": "remediated", "summary": "ok", "tests_run": ["pytest x"],
                               "pr_url": "https://github.com/acme/superset/pull/10", "blocker": None}},
        {"state": "open", "merged": False},
        states.NEEDS_REVIEW,
        base_ref="feature/side",
    )

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
