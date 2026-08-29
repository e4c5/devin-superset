#!/usr/bin/env python3
"""Optional terminal dashboard over the Ops Guard REST API.

The REST endpoints are the source of truth; this is a convenience view.

Usage: python scripts/dashboard.py [--base http://localhost:8000] [--once]
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=10) as resp:
        return json.loads(resp.read())


def render(base: str) -> None:
    metrics = _get(base, "/metrics")
    runs = _get(base, "/runs")["runs"]
    print("\033[2J\033[H", end="")
    print("Devin-Ops Guard\n" + "=" * 72)
    print(" ".join(f"{k}={v}" for k, v in metrics.items()))
    print("-" * 72)
    print(f"{'issue':>6}  {'state':<16} {'status':<18} pr")
    for r in runs:
        print(f"{r['issue_number']:>6}  {r['state']:<16} {str(r.get('status') or ''):<18} "
              f"{r.get('pr_url') or ''}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://localhost:8000")
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval", type=float, default=5.0)
    args = p.parse_args()
    while True:
        try:
            render(args.base)
        except Exception as exc:  # noqa: BLE001
            print("dashboard error:", exc)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
