#!/usr/bin/env python3
"""Send a correctly signed GitHub `issues` webhook to a local Ops Guard.

Validates the full dispatch + persistence path without a public tunnel.

Usage:
    GITHUB_WEBHOOK_SECRET=... python scripts/emit_sample_webhook.py \
        --repo your-org/superset --issue 42 --action labeled

    # replay the same delivery id to prove dedup:
    ... --delivery 11111111-1111-1111-1111-111111111111
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import urllib.request
import uuid


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000/webhook/github")
    p.add_argument("--repo", default=os.environ.get("TARGET_REPOSITORY", "your-org/superset"))
    p.add_argument("--issue", type=int, default=42)
    p.add_argument("--title", default="Fixture: bump vulnerable dependency")
    p.add_argument("--body", default="Expected behavior: ...\nAcceptance criteria: ...\nTest: pytest tests/unit_tests/foo_test.py")
    p.add_argument("--action", choices=["labeled", "opened"], default="labeled")
    p.add_argument("--label", default=os.environ.get("DEVIN_AUTOFIX_LABEL", "devin-autofix"))
    p.add_argument("--delivery", default=str(uuid.uuid4()))
    p.add_argument("--event", default="issues")
    p.add_argument("--secret", default=os.environ.get("GITHUB_WEBHOOK_SECRET", ""))
    p.add_argument("--bad-signature", action="store_true")
    args = p.parse_args()

    if not args.secret:
        print("GITHUB_WEBHOOK_SECRET is required (env or --secret)", file=sys.stderr)
        return 2

    labels = [{"name": args.label}] if args.action == "opened" else []
    payload = {
        "action": args.action,
        "issue": {
            "number": args.issue,
            "title": args.title,
            "body": args.body,
            "html_url": f"https://github.com/{args.repo}/issues/{args.issue}",
            "labels": labels,
        },
        "repository": {"full_name": args.repo, "id": 123456},
    }
    if args.action == "labeled":
        payload["label"] = {"name": args.label}

    raw = json.dumps(payload).encode()
    secret = (args.secret + "x") if args.bad_signature else args.secret
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()

    req = urllib.request.Request(
        args.url,
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": args.event,
            "X-GitHub-Delivery": args.delivery,
            "X-Hub-Signature-256": sig,
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            print(resp.status, resp.read().decode())
    except urllib.error.HTTPError as exc:
        print(exc.code, exc.read().decode())
        return 1
    print(f"delivery={args.delivery}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
