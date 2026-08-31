from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

from .paths import data_path

# --------------------------------------------------------------------------
# Append-only audit log (SoW Section 1.4.1 Part 4).
#
# First instantiated in Day 3.5 to record limitation-review decisions on
# pleadings (the trip + the user's confirmation). Phase 2 will extend the same
# log to record stripped CanLII citations. One JSON object per line, retained
# indefinitely, reviewable by the user.
# --------------------------------------------------------------------------


def audit_log_path() -> Path:
    """Location of the audit log. Overridable via MATTER_CLERK_AUDIT_LOG."""
    override = os.environ.get("MATTER_CLERK_AUDIT_LOG")
    if override:
        return Path(override)
    return data_path("logs/audit.jsonl")


def log_event(event: str, **fields) -> Path:
    """Append one audit record and return the log path. Never raises on the
    happy path of the caller — a logging failure should not lose the user's
    work, but we do not silently swallow it either (it surfaces in stderr)."""
    path = audit_log_path()
    record = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        # A missing audit record is a compliance failure (SoW 1.4.1 Part 4), so
        # never swallow this. But do not raise either: losing the user's draft
        # over a logging error would be worse. Surface it loudly on stderr,
        # including the unwritten record so it can be recovered by hand.
        print(
            f"AUDIT WRITE FAILED for event {event!r} -> {path}: {e!r}\n"
            f"  unwritten record: {json.dumps(record, ensure_ascii=False)}",
            file=sys.stderr,
            flush=True,
        )
    return path
