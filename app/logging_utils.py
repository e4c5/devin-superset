"""One JSON log line per state transition / notable event."""
from __future__ import annotations

import json
import logging
import time
from typing import Any


def configure() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


def event(logger: logging.Logger, kind: str, **fields: Any) -> None:
    logger.info(json.dumps({"ts": round(time.time(), 3), "event": kind, **fields}))
