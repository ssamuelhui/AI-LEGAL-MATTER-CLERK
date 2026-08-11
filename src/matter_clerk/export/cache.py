"""Transient server-side store for renderable results (Day 4d).

Why a module-level dict and not the Flask session: Flask's default session is a
signed COOKIE capped at roughly 4 KB. An ExportPayload with citations is far
larger, so the session is not a candidate regardless of convenience.

Why not SQLite: results are transient by design. A lawyer who closes the browser
and comes back should not find a stale answer waiting; the 30-minute TTL is a
feature, and persisting matter-derived text to a second on-disk store would
widen the confidential-data surface for no benefit.

Semantics that matter:
  * `get_result` is LOOKUP ONLY. Exporting Word and then PDF from one result
    page must yield both files, so a read never evicts. TTL expiry is the only
    removal path.
  * Eviction is lazy — swept on every put/get. No background thread, which
    matters given the deliberate `daemon_threads = False` shutdown semantics
    (see ARCHITECTURE 2026-06-04): there is nothing extra to join on Ctrl+C.
  * An RLock, not a Lock: `put`/`get` call `_sweep` while already holding the
    lock, and re-entrancy lets them share one implementation without a separate
    caller-holds-the-lock variant.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from .payload import EXCEL_TASKS, ExportPayload

log = logging.getLogger("matter_clerk.export")

TTL_SECONDS: int = 30 * 60
MAX_ENTRIES: int = 200
TOKEN_BYTES: int = 24

_LOCK = threading.RLock()


@dataclass
class _Entry:
    deadline: float  # time.monotonic() value past which this is expired
    payload: ExportPayload


# Ordered by insertion; re-inserted on read so the LRU cap evicts genuinely
# cold entries rather than merely old ones.
_CACHE: "OrderedDict[str, _Entry]" = OrderedDict()
_STATS = {"stored": 0, "hits": 0, "misses": 0, "expired": 0, "evicted": 0}


def _sweep() -> int:
    """Drop expired entries. Caller must hold the lock (RLock makes that safe
    from put/get). Returns the number removed."""
    now = time.monotonic()
    dead = [t for t, e in _CACHE.items() if e.deadline <= now]
    for t in dead:
        del _CACHE[t]
    if dead:
        _STATS["expired"] += len(dead)
        log.debug(f"Export cache: swept {len(dead)} expired result(s).")
    return len(dead)


def store_result(payload: ExportPayload) -> str:
    """Cache a result and return the token that addresses it."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    with _LOCK:
        _sweep()
        _CACHE[token] = _Entry(time.monotonic() + TTL_SECONDS, payload)
        _STATS["stored"] += 1
        while len(_CACHE) > MAX_ENTRIES:
            _CACHE.popitem(last=False)  # oldest/coldest first
            _STATS["evicted"] += 1
    return token


def get_result(token: str) -> ExportPayload | None:
    """Look up a cached result. Does NOT evict — see module docstring.

    Returns None for both "expired" and "never existed"; the endpoint renders
    one message for both, since the distinction tells the user nothing useful.
    """
    with _LOCK:
        _sweep()
        entry = _CACHE.get(token)
        if entry is None:
            _STATS["misses"] += 1
            return None
        _CACHE.move_to_end(token)  # mark as recently used
        _STATS["hits"] += 1
        return entry.payload


def list_export_formats(payload: ExportPayload) -> list[str]:
    """Formats offered for this payload's task, in button order. Word and PDF
    for every task; Excel only where the output is inherently tabular."""
    formats = ["docx", "pdf"]
    if payload.task in EXCEL_TASKS:
        formats.append("xlsx")
    return formats


def sweep() -> int:
    """Public reaper. Called lazily by put/get; exposed for tests."""
    with _LOCK:
        return _sweep()


def stats() -> dict[str, int]:
    with _LOCK:
        return dict(_STATS, size=len(_CACHE))


def clear() -> None:
    """Tests only."""
    with _LOCK:
        _CACHE.clear()
