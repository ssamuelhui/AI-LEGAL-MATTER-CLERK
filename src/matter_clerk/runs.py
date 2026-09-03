r"""Background run registry for exhaustive mode (Session 8).

Exhaustive runs are too slow to serve inside a request. Measured: 169.5 seconds
for a single-batch run over a 9-file matter, and a 28-file matter extrapolates
to six to nine minutes. A form POST that blocks that long is a browser timeout
waiting to happen, and it gives the lawyer nothing to look at.

So a run is a background thread plus a JSON file. The file is the source of
truth, not the thread:

  * the browser can be closed and reopened, and the run page still works
  * Flask can be restarted, and a run that was in flight is reported as
    INTERRUPTED rather than appearing to still be going
  * cancellation is a flag on disk, so it works from any tab

One run per matter at a time, enforced by a lock file. A second attempt returns
the URL of the run already going rather than an error -- a lawyer who clicks
twice, or comes back in another tab, wants to see the run, not be told off.

State is written with an atomic replace so a poll that lands mid-write reads
the previous complete state instead of half a file.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .paths import data_dir

log = logging.getLogger("matter_clerk.runs")

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"

TERMINAL = (DONE, FAILED, CANCELLED, INTERRUPTED)

# A run whose heartbeat is older than this, and which still claims to be
# running, belongs to a process that is gone.
#
# This MUST exceed the heartbeat interval by a wide margin. A single exhaustive
# batch runs for minutes with no progress callback in between -- measured 169.5s
# -- so an earlier version that only refreshed the timestamp at batch boundaries
# declared its own live run dead partway through the first call. The heartbeat
# thread below is the fix; this value is the backstop.
STALE_SECONDS = 120
HEARTBEAT_SECONDS = 20

# Progress callbacks and the heartbeat both write the same file from different
# threads.
_WRITE_LOCK = threading.Lock()


def _runs_dir() -> Path:
    d = data_dir() / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_path(run_id: str) -> Path:
    return _runs_dir() / f"{run_id}.json"


def _lock_path(matter_id: int) -> Path:
    return _runs_dir() / f"matter-{matter_id}.lock"


@dataclass
class RunState:
    run_id: str
    matter_id: int
    task: str
    mode: str
    model: str
    # Session 10: what the lawyer selected, when exhaustive coerced it to
    # something else. Empty when they match.
    model_requested: str = ""
    status: str = QUEUED
    created_at: str = ""
    updated_at: str = ""
    # progress
    batch: int = 0
    batches: int = 0
    files_total: int = 0
    current_files: list[str] = field(default_factory=list)
    # running tally -- shown live, because "here is what you have spent" answers
    # the lawyer's real question better than any pre-run estimate could
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    # outcome
    error: str = ""
    failed_batches: list[str] = field(default_factory=list)
    unreadable_files: list[str] = field(default_factory=list)
    collapsed_duplicates: int = 0
    scope_names: list[str] = field(default_factory=list)
    cancel_requested: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _write(state: RunState) -> None:
    """Atomically persist. A poll mid-write must never read a partial file.

    os.replace is atomic but not reliable on Windows: it intermittently raises
    PermissionError (WinError 5) when a scanner, indexer or a concurrent reader
    momentarily holds the destination. Observed during Session 8 testing, where
    it killed a run before its first batch. A bounded retry clears it; if it
    still will not go, fall back to writing in place, because a torn state file
    read by one poll is a far smaller problem than a run that dies at startup.
    """
    with _WRITE_LOCK:
        state.updated_at = _now()
        path = _run_path(state.run_id)
        tmp = path.with_suffix(".tmp")
        payload = state.to_json()
        tmp.write_text(payload, encoding="utf-8")

    for attempt in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
        except OSError:
            break

    try:
        path.write_text(payload, encoding="utf-8")
        tmp.unlink(missing_ok=True)
        log.warning(f"run {state.run_id}: atomic replace unavailable, wrote in place")
    except Exception as e:                                        # noqa: BLE001
        log.warning(f"run {state.run_id}: could not persist state: {e}")


def load(run_id: str) -> RunState | None:
    """Read a run's state, repairing the status of runs whose process died."""
    path = _run_path(run_id)
    if not path.is_file():
        return None
    raw = None
    for attempt in range(3):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            break
        except Exception:                                         # noqa: BLE001
            # Possibly caught mid-write by the in-place fallback above.
            time.sleep(0.05)
    if raw is None:
        return None
    known = {f for f in RunState.__dataclass_fields__}
    state = RunState(**{k: v for k, v in raw.items() if k in known})

    if state.status in (QUEUED, RUNNING):
        try:
            age = (dt.datetime.now(dt.timezone.utc)
                   - dt.datetime.fromisoformat(state.updated_at)).total_seconds()
        except Exception:                                         # noqa: BLE001
            age = 0
        if age > STALE_SECONDS:
            # The app was closed or crashed mid-run. Say so plainly rather than
            # leaving a progress bar that will never move.
            state.status = INTERRUPTED
            state.error = ("Matter Clerk closed while this analysis was running, "
                           "so it did not finish. Run it again.")
            _write(state)
            release(state.matter_id, state.run_id)
    return state


def result_path(run_id: str) -> Path:
    return _runs_dir() / f"{run_id}.result.json"


def save_result(run_id: str, payload: str) -> None:
    path = result_path(run_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def load_result(run_id: str) -> dict | None:
    path = result_path(run_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                             # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# Per-matter lock
# --------------------------------------------------------------------------
def active_run_for(matter_id: int) -> str | None:
    """The run id currently holding this matter, or None.

    Reads through `load`, so a lock left behind by a dead process is cleared as
    a side effect of discovering the run is interrupted.
    """
    lock = _lock_path(matter_id)
    if not lock.is_file():
        return None
    try:
        run_id = lock.read_text(encoding="utf-8").strip()
    except Exception:                                             # noqa: BLE001
        return None
    if not run_id:
        return None
    state = load(run_id)
    if state is None or state.status in TERMINAL:
        lock.unlink(missing_ok=True)
        return None
    return run_id


def acquire(matter_id: int, run_id: str) -> str | None:
    """Claim the matter. Returns the OTHER run's id if one already holds it."""
    existing = active_run_for(matter_id)
    if existing:
        return existing
    _lock_path(matter_id).write_text(run_id, encoding="utf-8")
    return None


def release(matter_id: int, run_id: str) -> None:
    lock = _lock_path(matter_id)
    try:
        if lock.is_file() and lock.read_text(encoding="utf-8").strip() == run_id:
            lock.unlink(missing_ok=True)
    except Exception:                                             # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------
def create(matter_id: int, task: str, mode: str, model: str,
           scope_names: list[str], model_requested: str = "") -> RunState:
    state = RunState(
        run_id=uuid.uuid4().hex[:16],
        matter_id=matter_id, task=task, mode=mode, model=model,
        model_requested=model_requested,
        created_at=_now(), files_total=len(scope_names),
        scope_names=list(scope_names),
    )
    _write(state)
    return state


def update(state: RunState, **fields) -> RunState:
    for k, v in fields.items():
        setattr(state, k, v)
    _write(state)
    return state


def request_cancel(run_id: str) -> bool:
    state = load(run_id)
    if state is None or state.status in TERMINAL:
        return False
    state.cancel_requested = True
    _write(state)
    return True


def cancel_requested(run_id: str) -> bool:
    """Read the flag from disk, so a cancel from another tab is seen."""
    path = _run_path(run_id)
    if not path.is_file():
        return False
    try:
        return bool(json.loads(path.read_text(encoding="utf-8")).get("cancel_requested"))
    except Exception:                                             # noqa: BLE001
        return False


def start(state: RunState, work) -> None:
    """Run `work(state)` on a daemon thread, keeping status and lock honest.

    `work` receives the RunState and should call `update(state, ...)` as it
    progresses. Whatever happens, the run reaches a terminal status and the
    matter lock is released -- an exception that left a matter locked forever
    would be worse than the failure that caused it.
    """
    stop_beat = threading.Event()

    def heartbeat() -> None:
        """Keep updated_at fresh while a long batch is in flight.

        Without this the staleness check cannot tell "a model call is taking
        three minutes" from "the application was closed", and it guesses wrong
        in the direction that loses the lawyer's run.
        """
        while not stop_beat.wait(HEARTBEAT_SECONDS):
            try:
                if state.status not in TERMINAL:
                    _write(state)
            except Exception:                                     # noqa: BLE001
                pass

    def runner() -> None:
        threading.Thread(target=heartbeat, daemon=True,
                         name=f"beat-{state.run_id}").start()
        try:
            update(state, status=RUNNING)
            work(state)
            if state.status not in TERMINAL:
                update(state, status=DONE)
        except Exception as e:                                    # noqa: BLE001
            log.exception("exhaustive run failed")
            try:
                update(state, status=FAILED, error=f"{type(e).__name__}: {e}")
            except Exception:                                     # noqa: BLE001
                pass
        finally:
            stop_beat.set()
            release(state.matter_id, state.run_id)

    threading.Thread(target=runner, daemon=True,
                     name=f"exhaustive-{state.run_id}").start()
