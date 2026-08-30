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
from datetime import datetime


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=10) as resp:
        return json.loads(resp.read())


def _dur(seconds) -> str:
    if seconds is None:
        return "—"
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def _num(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return str(v)


def render(base: str) -> None:
    m = _get(base, "/metrics")
    runs = _get(base, "/runs")["runs"]

    print("\033[2J\033[H", end="")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"Devin-Ops Guard — {stamp}")
    print("=" * 72)

    left = [
        ("remediated", m.get("remediated_total")),
        ("needs review", m.get("needs_review_total")),
        ("failed", m.get("failed_total")),
        ("active", m.get("active_runs")),
    ]
    right = [
        ("total jobs", m.get("total_jobs")),
        ("PRs opened", m.get("prs_opened_total")),
        ("reconciled", m.get("reconciled_total")),
    ]
    print()
    print(f"  {'Outcomes':<32}Pipeline")
    print(f"  {'-' * 26:<32}{'-' * 26}")
    for i in range(max(len(left), len(right))):
        lk, lv = left[i] if i < len(left) else ("", "")
        rk, rv = right[i] if i < len(right) else ("", "")
        lcell = f"{lk:<18}{_num(lv):>6}" if lk else ""
        rcell = f"{rk:<14}{_num(rv):>6}" if rk else ""
        print(f"  {lcell:<32}{rcell}".rstrip())

    print()
    print("  Timing & cost")
    print(f"  {'-' * 26}")
    print(f"  {'median time to PR':<20}{_dur(m.get('median_elapsed_seconds'))}")
    print(f"  {'ACUs / remediated':<20}{_num(m.get('acus_per_remediated_run'))}")

    print()
    print("-" * 72)
    print(f" {'issue':>5}  {'state':<13} {'status':<16} {'acus':>6}  pr")
    print("-" * 72)
    for r in runs:
        print(
            f" {r['issue_number']:>5}  {r['state']:<13} "
            f"{str(r.get('status') or ''):<16} {_num(r.get('acus_consumed')):>6}  "
            f"{r.get('pr_url') or ''}"
        )


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
