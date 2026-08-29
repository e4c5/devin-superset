"""Job state machine.

queued -> creating -> running -> {remediated | needs_review | failed}
                  \\-> creation_unknown -> {reconciled | needs_review}
"""
from __future__ import annotations

QUEUED = "queued"
CREATING = "creating"
RUNNING = "running"
CREATION_UNKNOWN = "creation_unknown"

REMEDIATED = "remediated"
NEEDS_REVIEW = "needs_review"
FAILED = "failed"
RECONCILED = "reconciled"

# States that hold an issue: at most one job per issue may be in one of these.
ACTIVE_STATES = (QUEUED, CREATING, RUNNING, CREATION_UNKNOWN)

# States the monitor still needs to poll / act on.
NON_TERMINAL_STATES = (QUEUED, CREATING, RUNNING, CREATION_UNKNOWN)

TERMINAL_STATES = (REMEDIATED, NEEDS_REVIEW, FAILED, RECONCILED)
