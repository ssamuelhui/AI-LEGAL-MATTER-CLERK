r"""Per-task cost records for firm expense tracking and client billing.

Session 11. One row per task run, written whatever the outcome.

WHY THE COST IS MEASURED, NOT COMPUTED
--------------------------------------
OpenRouter reports what it actually charged, in `usage.cost`, when a request
asks for it. That figure is recorded verbatim. A cost computed from a cached
price table would be wrong whenever prompt caching applied, whenever the router
sent the request to a different upstream, and whenever a price changed since
the catalogue was last fetched -- and it would be wrong silently.

v1.0.5 carried exactly that failure: `exhaustive.MODEL_PRICING` held three
models with an Opus-rate fallback, while Session 10 made all 425 models
selectable, so a cheap model displayed a cost around twenty times too high.

WHY matter_name IS DENORMALISED
-------------------------------
`matter_id` is a plain integer, deliberately NOT a foreign key. Session 10's
purge hard-deletes matters after their 30-day window; a real FK would either
block that purge or cascade the billing history away with it, and both are
wrong for an accounting record. The matter's name is copied onto the row at
write time so a purged matter still reads by name rather than by number.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import sqlite3

from .matters import connect as _connect_matters

log = logging.getLogger("matter_clerk.costs")

STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_costs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT    NOT NULL,
    matter_id        INTEGER,
    matter_name      TEXT,
    task_id          TEXT    NOT NULL,
    model_used       TEXT,
    input_tokens     INTEGER NOT NULL DEFAULT 0,
    output_tokens    INTEGER NOT NULL DEFAULT 0,
    cost_usd         REAL,
    duration_seconds REAL,
    was_exhaustive   INTEGER NOT NULL DEFAULT 0,
    status           TEXT    NOT NULL DEFAULT 'completed',
    detail           TEXT,
    calls            INTEGER NOT NULL DEFAULT 0,
    source           TEXT    NOT NULL DEFAULT 'measured',
    run_id           TEXT
);
CREATE INDEX IF NOT EXISTS idx_costs_time   ON task_costs(timestamp);
CREATE INDEX IF NOT EXISTS idx_costs_matter ON task_costs(matter_id);
"""


def init(conn: sqlite3.Connection) -> None:
    """Create the table if absent. Safe to call on every connection."""
    conn.executescript(_SCHEMA)
    conn.commit()


def connect() -> sqlite3.Connection:
    conn = _connect_matters()
    init(conn)
    return conn


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def record(
    *,
    task_id: str,
    matter_id: int | None,
    matter_name: str | None,
    model_used: str | None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float | None = 0.0,
    duration_seconds: float | None = None,
    was_exhaustive: bool = False,
    status: str = STATUS_COMPLETED,
    detail: str = "",
    calls: int = 0,
    source: str = "measured",
    run_id: str | None = None,
    timestamp: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> int | None:
    """Write one cost row. Never raises -- a billing record that fails to save
    must not also lose the lawyer's result."""
    own = conn is None
    try:
        conn = conn or connect()
        cur = conn.execute(
            "INSERT INTO task_costs (timestamp, matter_id, matter_name, task_id,"
            " model_used, input_tokens, output_tokens, cost_usd,"
            " duration_seconds, was_exhaustive, status, detail, calls, source,"
            " run_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (timestamp or _now(), matter_id, matter_name, task_id, model_used,
             int(input_tokens or 0), int(output_tokens or 0), cost_usd,
             duration_seconds, 1 if was_exhaustive else 0, status, detail or "",
             int(calls or 0), source, run_id),
        )
        conn.commit()
        return cur.lastrowid
    except Exception as e:                                        # noqa: BLE001
        log.warning(f"could not record task cost: {type(e).__name__}: {e}")
        return None
    finally:
        if own and conn is not None:
            conn.close()


def record_from_accumulator(
    acc, *, matter_name: str | None, duration_seconds: float | None,
    was_exhaustive: bool = False, status: str = STATUS_COMPLETED,
    detail: str = "", run_id: str | None = None,
) -> int | None:
    """Write the row for a finished run from its accumulator.

    `cost_unavailable` becomes a NULL cost rather than a partial total: a
    figure that is quietly short is worse for billing than an honest blank.
    """
    return record(
        task_id=acc.task, matter_id=acc.matter_id, matter_name=matter_name,
        model_used=(acc.models_used[0] if acc.models_used else acc.model),
        input_tokens=acc.input_tokens, output_tokens=acc.output_tokens,
        cost_usd=None if acc.cost_unavailable else round(acc.cost_usd, 6),
        duration_seconds=duration_seconds, was_exhaustive=was_exhaustive,
        status=status, detail=detail, calls=acc.calls, run_id=run_id,
    )


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------
PERIODS = {
    "7": "Last 7 days",
    "30": "Last 30 days",
    "all": "All time",
}


def _cutoff(period: str) -> str | None:
    if period in ("7", "30"):
        days = int(period)
        return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    return None


def query(
    conn: sqlite3.Connection,
    matter_id: int | None = None,
    period: str = "30",
    sort: str = "timestamp",
    direction: str = "desc",
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    """Cost rows joined to matter names, including deleted matters.

    The join is LEFT and unfiltered by `deleted_at`, so a soft-deleted matter's
    spending still appears -- the money was spent whatever became of the matter
    afterwards. Where the matter has been purged entirely the join finds
    nothing and the row falls back to the name stored on it.
    """
    sql = [
        "SELECT c.*, m.name AS live_name, m.deleted_at AS matter_deleted_at",
        "FROM task_costs c LEFT JOIN matters m ON m.id = c.matter_id",
        "WHERE 1=1",
    ]
    params: list = []
    if matter_id is not None:
        sql.append("AND c.matter_id = ?")
        params.append(matter_id)
    if date_from:
        sql.append("AND c.timestamp >= ?")
        params.append(date_from)
    if date_to:
        sql.append("AND c.timestamp <= ?")
        params.append(date_to + "T23:59:59+00:00")
    if not date_from and not date_to:
        cutoff = _cutoff(period)
        if cutoff:
            sql.append("AND c.timestamp >= ?")
            params.append(cutoff)

    columns = {"timestamp": "c.timestamp", "matter": "c.matter_name",
               "task": "c.task_id", "model": "c.model_used", "cost": "c.cost_usd"}
    order = columns.get(sort, "c.timestamp")
    sql.append(f"ORDER BY {order} {'ASC' if direction == 'asc' else 'DESC'}")

    rows = conn.execute(" ".join(sql), params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["display_matter"] = _display_matter(d)
        out.append(d)
    return out


def _display_matter(row: dict) -> str:
    """How a matter reads in the log, whatever has happened to it since."""
    if row.get("matter_id") is None:
        return "(no matter)"
    if row.get("live_name"):
        if row.get("matter_deleted_at"):
            return f"{row['live_name']} (deleted)"
        return row["live_name"]
    # Purged entirely: the stored name is the only record left, which is why
    # it is stored.
    if row.get("matter_name"):
        return f"{row['matter_name']} (removed)"
    return f"Matter {row['matter_id']} (removed)"


def totals(rows: list[dict]) -> tuple[float, int, int]:
    """(total cost, number of rows, number with unknown cost)."""
    total = sum(r["cost_usd"] or 0.0 for r in rows)
    unknown = sum(1 for r in rows if r["cost_usd"] is None)
    return round(total, 4), len(rows), unknown


def matter_options(conn: sqlite3.Connection) -> list[dict]:
    """Matters that have cost rows, for the filter dropdown."""
    rows = conn.execute(
        """
        SELECT c.matter_id AS id, m.name AS live_name,
               m.deleted_at AS matter_deleted_at,
               MAX(c.matter_name) AS matter_name, COUNT(*) AS n
        FROM task_costs c LEFT JOIN matters m ON m.id = c.matter_id
        WHERE c.matter_id IS NOT NULL
        GROUP BY c.matter_id
        ORDER BY COALESCE(m.name, c.matter_name)
        """
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # _display_matter reads `matter_id`; this query names the column `id`.
        d["matter_id"] = d.get("id")
        out.append({"id": d["id"], "label": _display_matter(d), "n": r["n"]})
    return out


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------
CSV_COLUMNS = [
    "timestamp_utc", "date_local", "matter_id", "matter_name",
    "task_id", "model", "exhaustive", "status",
    "input_tokens", "output_tokens", "cost_usd", "duration_seconds",
]


def to_csv(rows: list[dict]) -> str:
    """Firm-readable CSV of the current view.

    Column order runs identity -> classification -> measurement, which is how
    an accountant reads a row left to right. `cost_usd` is a plain decimal with
    no currency symbol, to four places, so sub-cent runs do not round to zero.
    Both a full UTC timestamp and a local date are present: one sorts, the
    other filters in a spreadsheet.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for r in rows:
        ts = r.get("timestamp") or ""
        writer.writerow([
            ts,
            ts[:10],
            r.get("matter_id") if r.get("matter_id") is not None else "",
            r.get("display_matter") or r.get("matter_name") or "",
            r.get("task_id") or "",
            r.get("model_used") or "",
            "true" if r.get("was_exhaustive") else "false",
            r.get("status") or "",
            r.get("input_tokens") or 0,
            r.get("output_tokens") or 0,
            "" if r.get("cost_usd") is None else f"{r['cost_usd']:.4f}",
            "" if r.get("duration_seconds") is None
            else f"{r['duration_seconds']:.1f}",
        ])
    return buf.getvalue()


def csv_filename() -> str:
    return f"task_costs_{dt.date.today().isoformat()}.csv"
