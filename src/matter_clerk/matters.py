from __future__ import annotations

import datetime as dt
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .paths import data_path

# --------------------------------------------------------------------------
# Matter persistence (Day 4a).
#
# A "matter" is the set of files for one legal case (the Imperial Plaza condo
# dispute; the Cresthaven case). Files belong to exactly ONE matter — a strict
# one-to-many relationship, so no join table. If the same file is relevant to
# two matters the user uploads it twice; re-embedding is cheap (local CPU) and
# the conceptual clarity is worth the duplication.
#
# Storage is stdlib sqlite3 (no new dependency) at the project root. The schema
# is intentionally minimal but designed to extend to Phase-3 feedback storage
# without re-migrating these two tables: feedback will be a NEW table that
# references files(id) / matters(id). PRAGMA user_version is the anchor for any
# future additive migration.
# --------------------------------------------------------------------------

SCHEMA_VERSION = 1

# The exact DDL run by init_db(). Reviewed and approved before first run.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS matters (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    description     TEXT,
    created_at      TEXT    NOT NULL,
    modified_at     TEXT    NOT NULL,
    last_queried_at TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    matter_id      INTEGER NOT NULL REFERENCES matters(id),
    filename       TEXT    NOT NULL,
    file_type      TEXT    NOT NULL,
    content_sha256 TEXT    NOT NULL,
    collection     TEXT    NOT NULL,
    stored_path    TEXT    NOT NULL,
    ingest_status  TEXT    NOT NULL,
    ingest_error   TEXT,
    ingested_at    TEXT,
    created_at     TEXT    NOT NULL,
    UNIQUE (matter_id, content_sha256)
);

CREATE INDEX IF NOT EXISTS idx_files_matter ON files(matter_id);
"""


# --------------------------------------------------------------------------
# Errors — each carries a specific, user-facing message (no generic 500s).
# --------------------------------------------------------------------------
class MatterError(RuntimeError):
    """Base for matter-layer errors that callers render to the user verbatim."""


class MatterNotFound(MatterError):
    def __init__(self, matter_id: int) -> None:
        super().__init__(f"No matter with id {matter_id}.")
        self.matter_id = matter_id


class DuplicateMatterName(MatterError):
    def __init__(self, name: str) -> None:
        super().__init__(f"A matter named {name!r} already exists.")
        self.name = name


class DuplicateFileInMatter(MatterError):
    """Raised when a file whose content hash is already in the matter is added
    again. Detected by content hash, not filename — the same bytes under a
    different name is still a duplicate."""

    def __init__(self, filename: str) -> None:
        super().__init__(
            f"File {filename!r} is already in this matter - duplicate detected "
            f"by content hash."
        )
        self.filename = filename


class FileNotInMatter(MatterError):
    """Raised when a file_id does not belong to the matter it was requested
    under (e.g. a tampered /matters/<id>/query form). A refusal, not a 500."""

    def __init__(self, file_id: int, matter_id: int) -> None:
        super().__init__(
            f"File {file_id} does not belong to matter {matter_id}."
        )
        self.file_id = file_id
        self.matter_id = matter_id


# --------------------------------------------------------------------------
# Row views
# --------------------------------------------------------------------------
@dataclass
class Matter:
    id: int
    name: str
    description: str | None
    created_at: str
    modified_at: str
    last_queried_at: str | None
    file_count: int = 0  # populated by list_matters / get_matter


@dataclass
class MatterFile:
    id: int
    matter_id: int
    filename: str
    file_type: str
    content_sha256: str
    collection: str
    stored_path: str
    ingest_status: str
    ingest_error: str | None
    ingested_at: str | None
    created_at: str


# --------------------------------------------------------------------------
# Paths & connection
# --------------------------------------------------------------------------
def db_path() -> Path:
    r"""Location of the SQLite DB. Overridable via MATTER_CLERK_DB (tests).

    Phase 3 Session 3: resolves under paths.data_dir() rather than the repo
    root. In a source checkout those are the same directory, so this is a
    no-op for development; in a bundle it moves the DB out of the read-only
    install directory and into %LOCALAPPDATA%\MatterClerk."""
    override = os.environ.get("MATTER_CLERK_DB")
    return Path(override) if override else data_path("matter_clerk.db")


def matters_store_root() -> Path:
    """Root of the on-disk matter file store. Overridable via
    MATTER_CLERK_MATTERS_DIR (tests)."""
    override = os.environ.get("MATTER_CLERK_MATTERS_DIR")
    return Path(override) if override else data_path("data/matters")


def stored_path_for(matter_id: int, sha256: str, suffix: str) -> Path:
    """data/matters/<matter_id>/<sha16><suffix> — the concrete artifact a lawyer
    would recognise as the matter folder, and the path the query route hands to
    the pipeline after the upload request is gone."""
    suffix = suffix if suffix.startswith(".") else f".{suffix}"
    return matters_store_root() / str(matter_id) / f"{sha256[:16]}{suffix}"


def collection_name(matter_id: int, sha256: str) -> str:
    """Matter-scoped collection name: identical content in two matters yields
    two distinct collections (m<matter_id>-<sha16>), unlike the ad-hoc path's
    content-only day1-<sha16>. Persisted in files.collection at ingest, so the
    naming scheme can change later without a DB migration."""
    return f"m{matter_id}-{sha256[:16]}"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    """Open (creating if needed) the matter DB with the schema applied.

    Foreign keys are enforced per-connection (SQLite default is off). The
    schema uses IF NOT EXISTS so connect() is safe to call on every request."""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables/indices if absent and stamp user_version once."""
    conn.executescript(_SCHEMA)
    if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


# --------------------------------------------------------------------------
# Matter operations
# --------------------------------------------------------------------------
def create_matter(
    conn: sqlite3.Connection, name: str, description: str | None = None
) -> Matter:
    name = name.strip()
    if not name:
        raise MatterError("Matter name cannot be empty.")
    now = _now()
    try:
        cur = conn.execute(
            "INSERT INTO matters (name, description, created_at, modified_at) "
            "VALUES (?, ?, ?, ?)",
            (name, (description or "").strip() or None, now, now),
        )
    except sqlite3.IntegrityError:
        raise DuplicateMatterName(name)
    conn.commit()
    return get_matter(conn, cur.lastrowid)


def get_matter(conn: sqlite3.Connection, matter_id: int) -> Matter:
    row = conn.execute(
        """
        SELECT m.*, COUNT(f.id) AS file_count
        FROM matters m
        LEFT JOIN files f ON f.matter_id = m.id
        WHERE m.id = ?
        GROUP BY m.id
        """,
        (matter_id,),
    ).fetchone()
    if row is None:
        raise MatterNotFound(matter_id)
    return _matter_from_row(row)


def get_matter_by_name(conn: sqlite3.Connection, name: str) -> Matter | None:
    row = conn.execute(
        """
        SELECT m.*, COUNT(f.id) AS file_count
        FROM matters m
        LEFT JOIN files f ON f.matter_id = m.id
        WHERE m.name = ?
        GROUP BY m.id
        """,
        (name.strip(),),
    ).fetchone()
    return _matter_from_row(row) if row else None


def list_matters(conn: sqlite3.Connection) -> list[Matter]:
    """All matters, most-recently-active first (last_queried_at, then created)."""
    rows = conn.execute(
        """
        SELECT m.*, COUNT(f.id) AS file_count
        FROM matters m
        LEFT JOIN files f ON f.matter_id = m.id
        GROUP BY m.id
        ORDER BY COALESCE(m.last_queried_at, m.created_at) DESC
        """
    ).fetchall()
    return [_matter_from_row(r) for r in rows]


def touch_last_queried(conn: sqlite3.Connection, matter_id: int) -> None:
    conn.execute(
        "UPDATE matters SET last_queried_at = ?, modified_at = ? WHERE id = ?",
        (_now(), _now(), matter_id),
    )
    conn.commit()


# --------------------------------------------------------------------------
# File operations
# --------------------------------------------------------------------------
def add_file_pending(
    conn: sqlite3.Connection,
    matter_id: int,
    filename: str,
    file_type: str,
    content_sha256: str,
    collection: str,
    stored_path: str,
) -> MatterFile:
    """Insert a file row as 'pending' before ingestion. Raises
    DuplicateFileInMatter if this content is already in the matter."""
    get_matter(conn, matter_id)  # raises MatterNotFound if absent
    now = _now()
    try:
        cur = conn.execute(
            """
            INSERT INTO files (
                matter_id, filename, file_type, content_sha256, collection,
                stored_path, ingest_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (matter_id, filename, file_type, content_sha256, collection,
             stored_path, now),
        )
    except sqlite3.IntegrityError:
        raise DuplicateFileInMatter(filename)
    conn.execute(
        "UPDATE matters SET modified_at = ? WHERE id = ?", (now, matter_id)
    )
    conn.commit()
    return get_file(conn, cur.lastrowid)


# --------------------------------------------------------------------------
# Ingest status vocabulary (Session 6a)
#
#   ingested         indexed and queryable
#   ocr_low_quality  indexed and queryable, but the OCR output looks too poor
#                    to answer from -- a warning, not an exclusion
#   failed_no_text   nothing searchable was produced; NOT queryable
#   failed           ingestion raised
#
# Only the first two are queryable. `failed_no_text` is separate from `failed`
# because it is actionable in a specific way -- re-scan and re-upload -- and
# because the migration backfill needs to distinguish files it demoted from
# files that failed outright at upload time.
# --------------------------------------------------------------------------
QUERYABLE_STATUSES = ("ingested", "ocr_low_quality")

STATUS_LABELS = {
    "ingested": "Ready",
    "ocr_low_quality": "Poor scan quality",
    "failed_no_text": "No readable text",
    "failed": "Ingest failed",
    "pending": "Not yet processed",
}


def is_queryable(status: str) -> bool:
    return status in QUERYABLE_STATUSES


def mark_file_ingested(
    conn: sqlite3.Connection, file_id: int, status: str = "ingested",
    note: str | None = None,
) -> None:
    """Record a successful ingest. `status` may be 'ingested' or
    'ocr_low_quality'; the latter stays queryable but carries a warning."""
    if status not in QUERYABLE_STATUSES:
        raise ValueError(f"not a queryable status: {status!r}")
    conn.execute(
        "UPDATE files SET ingest_status = ?, ingested_at = ?, "
        "ingest_error = ? WHERE id = ?",
        (status, _now(), (note or None), file_id),
    )
    conn.commit()


def mark_file_no_text(conn: sqlite3.Connection, file_id: int, note: str) -> None:
    """Mark a file as having produced nothing searchable."""
    conn.execute(
        "UPDATE files SET ingest_status = 'failed_no_text', ingest_error = ? "
        "WHERE id = ?",
        (note[:2000], file_id),
    )
    conn.commit()


def delete_file(conn: sqlite3.Connection, file_id: int) -> None:
    """Remove a file row from a matter. The stored artifact and any vector
    collection are the caller's to clean up."""
    row = get_file(conn, file_id)
    conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
    if row is not None:
        conn.execute(
            "UPDATE matters SET modified_at = ? WHERE id = ?", (_now(), row.matter_id)
        )
    conn.commit()


def mark_file_failed(conn: sqlite3.Connection, file_id: int, error: str) -> None:
    conn.execute(
        "UPDATE files SET ingest_status = 'failed', ingest_error = ? WHERE id = ?",
        (error[:2000], file_id),
    )
    conn.commit()


def list_files(conn: sqlite3.Connection, matter_id: int) -> list[MatterFile]:
    rows = conn.execute(
        "SELECT * FROM files WHERE matter_id = ? ORDER BY created_at, id",
        (matter_id,),
    ).fetchall()
    return [_file_from_row(r) for r in rows]


def get_file(conn: sqlite3.Connection, file_id: int) -> MatterFile:
    row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        raise MatterError(f"No file with id {file_id}.")
    return _file_from_row(row)


def get_file_in_matter(
    conn: sqlite3.Connection, matter_id: int, file_id: int
) -> MatterFile:
    """Fetch a file and assert it belongs to `matter_id`. Guards the
    /matters/<id>/query route against a tampered file_id pointing at another
    matter — raises FileNotInMatter (a refusal), never a 500."""
    f = get_file(conn, file_id)
    if f.matter_id != matter_id:
        raise FileNotInMatter(file_id, matter_id)
    return f


# --------------------------------------------------------------------------
# Row mappers
# --------------------------------------------------------------------------
def _matter_from_row(row: sqlite3.Row) -> Matter:
    return Matter(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        created_at=row["created_at"],
        modified_at=row["modified_at"],
        last_queried_at=row["last_queried_at"],
        file_count=row["file_count"] if "file_count" in row.keys() else 0,
    )


def _file_from_row(row: sqlite3.Row) -> MatterFile:
    return MatterFile(
        id=row["id"],
        matter_id=row["matter_id"],
        filename=row["filename"],
        file_type=row["file_type"],
        content_sha256=row["content_sha256"],
        collection=row["collection"],
        stored_path=row["stored_path"],
        ingest_status=row["ingest_status"],
        ingest_error=row["ingest_error"],
        ingested_at=row["ingested_at"],
        created_at=row["created_at"],
    )


# --------------------------------------------------------------------------
# Date-prefix parsing and file ordering (Session 7)
#
# Lawyers name matter documents by date. The real conventions in the pilot
# matter, which these rules are calibrated against rather than guessed from:
#
#   2026-03-27 - Technician Report Form.pdf
#   2026-01-21 - 2026-03-26 - Condo manage email re inspection.pdf
#   2024-04-01 to 2026-04-30 - email exchange re. Heat pump.pdf
#   Condo Bylaw 6.pdf                     (undated -> alphabetical)
#
# Note the third and second forms: a date RANGE, written either with "to" or
# with a hyphen -- and in the hyphen case the same " - " separator divides the
# two dates AND the date block from the description. Sorting is by the START
# date, so the range never needs to be disentangled to sort correctly; it is
# recognised anyway so the parsed value is honest and a future date column has
# real data to show.
#
# What deliberately does NOT parse:
#   3-15-24_x.pdf     US order. Read as YY-MM-DD it is month 15 -> rejected.
#                     US order is never attempted: 03-04-05 is valid in three
#                     different orderings, and silently guessing wrong in a
#                     legal chronology is worse than not sorting at all.
#   letter_24-03-15   Not a prefix. A mid-name number is as likely to be a
#                     court file number, a docket, or an amount.
#   23_march_x.pdf    Word months: is 23 a day or a year?
# --------------------------------------------------------------------------
import re as _re

# YYYY-MM-DD or YY-MM-DD, separated by - _ or . (the last is a common Windows
# convention). Anchored at the start: this is a date PREFIX parser.
_DATE_RE = _re.compile(
    r"^(?P<y>\d{4}|\d{2})[-_.](?P<m>\d{1,2})[-_.](?P<d>\d{1,2})(?P<rest>.*)$"
)

# What may follow a date for it to count as a prefix rather than a coincidence:
# whitespace, a separator, or the end of the name.
_BOUNDARY = _re.compile(r"^([\s\-_.]|$)")

# " to " / " - " / "_to_" between two dates.
_RANGE_JOIN = _re.compile(r"^\s*(to|-|until|through)\s*", _re.IGNORECASE)

# Two-digit years pivot at 70, the usual POSIX convention: 00-69 -> 2000s,
# 70-99 -> 1900s. A matter may well reference a 1998 document.
_YEAR_PIVOT = 70


def _to_date(y: str, m: str, d: str):
    """Build a date, or None if the components are not a real calendar date."""
    import datetime as _dt

    year = int(y)
    if len(y) == 2:
        year += 2000 if year < _YEAR_PIVOT else 1900
    try:
        return _dt.date(year, int(m), int(d))
    except ValueError:
        return None          # 24-02-30, 24-13-01, and every other fake date


def parse_date_prefix(filename: str):
    """Parse a leading date (or date range) from a filename.

    Returns (start_date, end_date) where end_date is None for a single date,
    or None when the filename carries no usable date prefix.
    """
    if not filename:
        return None

    stem = filename.rsplit(".", 1)[0] if "." in filename else filename

    m = _DATE_RE.match(stem)
    if not m:
        return None
    start = _to_date(m.group("y"), m.group("m"), m.group("d"))
    if start is None:
        return None

    rest = m.group("rest")
    if not _BOUNDARY.match(rest):
        # e.g. "2024-03-15x" -- a date-looking run inside a longer token.
        return None

    # Optional second date, making this a range. The sort key is the start
    # either way, so failing to spot the range costs nothing but accuracy.
    join = _RANGE_JOIN.match(rest)
    if join:
        m2 = _DATE_RE.match(rest[join.end():])
        if m2:
            end = _to_date(m2.group("y"), m2.group("m"), m2.group("d"))
            if end is not None and _BOUNDARY.match(m2.group("rest")):
                return (start, end)
    return (start, None)


SORT_ORDERS = ("oldest", "newest", "upload")
DEFAULT_SORT = "oldest"

SORT_LABELS = {
    "oldest": "Oldest first",
    "newest": "Newest first",
    "upload": "Upload order",
}


def sort_files(files: list[MatterFile], order: str = DEFAULT_SORT) -> list[MatterFile]:
    """Order a matter's files for display and for selection.

    Dated files sort by their parsed date; undated files sort alphabetically
    by filename. Dated files come first as a group, because a chronology with
    undated material interleaved by name would read as though the names were
    dates. Reversing to "newest first" reverses the dated group only -- the
    alphabetical tail stays A-Z, since reverse-alphabetical is not a thing a
    lawyer asked for and reads as a bug.
    """
    if order not in SORT_ORDERS:
        order = DEFAULT_SORT
    if order == "upload":
        return list(files)          # the SQL order: created_at, id

    dated: list[tuple] = []
    undated: list[tuple] = []
    for f in files:
        parsed = parse_date_prefix(f.filename)
        if parsed is None:
            undated.append(((f.filename or "").lower(), f.id, f))
        else:
            dated.append((parsed[0], (f.filename or "").lower(), f.id, f))

    dated.sort(key=lambda t: (t[0], t[1], t[2]), reverse=(order == "newest"))
    undated.sort(key=lambda t: (t[0], t[1]))
    return [t[-1] for t in dated] + [t[-1] for t in undated]
