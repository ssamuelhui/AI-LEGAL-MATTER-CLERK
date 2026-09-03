r"""Startup migrations, health backfill and the user-facing diagnostic report.

Session 6a. Three related jobs that all answer the same question -- "does the
matter manifest still match what is actually in the vector store?" -- from
three angles:

  run_startup_migrations()  once per install, heal manifests that claim a file
                            is ingested when its collection is gone or broken
  build_diagnostic_report() on demand, a STRUCTURE-ONLY snapshot a lawyer can
                            send us without disclosing a word of client content
  notices                   a tiny queue so a migration that runs before the
                            browser is open can still tell the user what it did

Nothing here may prevent the application from starting. A migration that
cannot run is retried next launch; a store that will not open is left to the
existing store-health banner. Turning a degraded install into a dead one would
be a strictly worse outcome than the bug being fixed.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import platform
import sqlite3
import sys
from pathlib import Path

from . import matters
from .paths import data_dir

log = logging.getLogger("matter_clerk.maintenance")

# Bumped when a new one-time migration is added. Each gets its own marker file,
# so adding one never re-runs the others.
MIGRATION_BACKFILL_INGEST_STATUS = "0001_backfill_ingest_status"

EMBED_DIM = 384


# --------------------------------------------------------------------------
# Notices -- a one-shot message queue rendered by the web UI
# --------------------------------------------------------------------------
def _notices_path() -> Path:
    return data_dir() / "notices.json"


def push_notice(kind: str, message: str) -> None:
    """Queue a message for the next page the user opens.

    A file rather than a database column: the migration runs before Flask does,
    may run when no browser is open at all, and must not require a schema
    change to a table the migration is itself repairing.
    """
    try:
        path = _notices_path()
        existing = []
        if path.is_file():
            existing = json.loads(path.read_text(encoding="utf-8")) or []
        existing.append({
            "kind": kind,
            "message": message,
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
        })
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    except Exception as e:                                        # noqa: BLE001
        log.warning(f"could not queue notice: {e}")


def take_notices() -> list[dict]:
    """Return queued notices and clear them. Shown once, then gone."""
    path = _notices_path()
    if not path.is_file():
        return []
    try:
        notices = json.loads(path.read_text(encoding="utf-8")) or []
        path.unlink(missing_ok=True)
        return notices
    except Exception:                                             # noqa: BLE001
        path.unlink(missing_ok=True)
        return []


# --------------------------------------------------------------------------
# One-time migrations
# --------------------------------------------------------------------------
def _marker(name: str) -> Path:
    return data_dir() / "migrations" / f"{name}.done"


def _already_run(name: str) -> bool:
    return _marker(name).is_file()


def _record_run(name: str, summary: str) -> None:
    m = _marker(name)
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text(
        f"{dt.datetime.now(dt.timezone.utc).isoformat()}\n{summary}\n",
        encoding="utf-8",
    )


def backfill_ingest_status() -> dict:
    """Reconcile files marked queryable against the vector store.

    A file whose collection is missing, empty, or unreadable is demoted to
    'failed_no_text' so it is excluded from retrieval and shown to the user as
    needing re-ingestion. This is what heals an install that is already broken:
    without it, those files keep being handed to the query path forever.

    Returns a summary dict; raises nothing the caller must handle.
    """
    from .vectorstore import connect, probe_collection

    result = {"checked": 0, "demoted": 0, "files": [], "error": None}

    try:
        client = connect()
    except Exception as e:                                        # noqa: BLE001
        # The store itself will not open. Leave the manifest alone -- demoting
        # every file in every matter because Chroma is temporarily unavailable
        # would be far more destructive than the problem.
        result["error"] = f"vector store unavailable: {type(e).__name__}: {e}"
        return result

    db = matters.db_path()
    if not db.is_file():
        return result

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, matter_id, filename, collection, ingest_status "
            "FROM files WHERE ingest_status IN ('ingested', 'ocr_low_quality')"
        ).fetchall()

        for row in rows:
            coll = row["collection"]
            if not coll:
                continue
            result["checked"] += 1

            state = probe_collection(client, coll, EMBED_DIM)
            if state == "ok":
                continue

            reason = {
                "missing": "Its search index is missing.",
                "empty": "Its search index contains no text.",
                "unreadable": "Its search index could not be read.",
            }.get(state, "Its search index is unusable.")

            conn.execute(
                "UPDATE files SET ingest_status = 'failed_no_text', "
                "ingest_error = ? WHERE id = ?",
                (f"{reason} Re-upload this file to make it searchable again.",
                 row["id"]),
            )
            result["demoted"] += 1
            result["files"].append(
                {"matter_id": row["matter_id"], "filename": row["filename"],
                 "state": state}
            )
        conn.commit()
    finally:
        conn.close()

    return result


def run_startup_migrations() -> None:
    """Run any pending one-time migrations. Never raises."""
    try:
        if _already_run(MIGRATION_BACKFILL_INGEST_STATUS):
            return

        log.info("Checking matter files against the search index ...")
        result = backfill_ingest_status()

        if result["error"]:
            # Not marked done -- retried next launch, when the store may open.
            log.warning(f"migration deferred: {result['error']}")
            return

        _record_run(
            MIGRATION_BACKFILL_INGEST_STATUS,
            f"checked={result['checked']} demoted={result['demoted']}",
        )

        if result["demoted"]:
            names = ", ".join(f["filename"] for f in result["files"][:5])
            more = (f" and {len(result['files']) - 5} more"
                    if len(result["files"]) > 5 else "")
            push_notice(
                "warning",
                f"{result['demoted']} file(s) in your matters could not be "
                f"read from the search index and have been marked as needing "
                f"re-upload ({names}{more}). Open the matter to review and "
                f"re-upload them. Your original documents were not changed.",
            )
            log.warning(
                f"marked {result['demoted']} file(s) as needing re-ingestion"
            )
        else:
            log.info(f"search index healthy ({result['checked']} file(s) checked)")

    except Exception as e:                                        # noqa: BLE001
        # Startup must never die here.
        log.warning(f"startup migration failed, continuing: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------
# Diagnostic report
# --------------------------------------------------------------------------
def build_diagnostic_report() -> dict:
    """A structure-only snapshot of this installation.

    DELIBERATELY EXCLUDES ALL CLIENT CONTENT. No document text, no chunk text,
    no matter names, no filenames, no API keys, no file paths that contain a
    client's name. Filenames are reduced to their extension and a length; the
    report is meant to be emailed to us by a lawyer whose material is
    privileged, and it has to be safe to send without anyone reading it first.
    """
    from . import __version__

    report: dict = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "app_version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "matters": [],
        "store": {},
        "migrations": [],
        "errors": [],
    }

    try:
        import chromadb

        report["chromadb_version"] = getattr(chromadb, "__version__", "unknown")
    except Exception:                                             # noqa: BLE001
        report["chromadb_version"] = "unavailable"

    # --- migration markers ---
    try:
        mig = data_dir() / "migrations"
        if mig.is_dir():
            report["migrations"] = sorted(p.stem for p in mig.glob("*.done"))
    except Exception as e:                                        # noqa: BLE001
        report["errors"].append(f"migrations: {type(e).__name__}")

    # --- vector store ---
    client = None
    try:
        from .vectorstore import connect, default_store_path

        store = default_store_path()
        report["store"]["path_exists"] = store.is_dir()
        if store.is_dir():
            report["store"]["sqlite_bytes"] = (
                (store / "chroma.sqlite3").stat().st_size
                if (store / "chroma.sqlite3").is_file() else 0
            )
            report["store"]["segment_dirs"] = sum(
                1 for p in store.iterdir() if p.is_dir()
            )
        client = connect()
        report["store"]["opens"] = True
    except Exception as e:                                        # noqa: BLE001
        report["store"]["opens"] = False
        report["store"]["error"] = f"{type(e).__name__}: {e}"

    # --- matters and files, structure only ---
    try:
        db = matters.db_path()
        report["db_exists"] = db.is_file()
        if db.is_file():
            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            try:
                for m in conn.execute("SELECT id FROM matters ORDER BY id"):
                    files = conn.execute(
                        "SELECT filename, collection, ingest_status "
                        "FROM files WHERE matter_id = ? ORDER BY id",
                        (m["id"],),
                    ).fetchall()
                    entry = {"matter_id": m["id"], "file_count": len(files),
                             "files": []}
                    for f in files:
                        item = {
                            # Extension and name length only -- never the name.
                            "ext": Path(f["filename"] or "").suffix.lower(),
                            "name_len": len(f["filename"] or ""),
                            "status": f["ingest_status"],
                            "has_collection": bool(f["collection"]),
                        }
                        if client is not None and f["collection"]:
                            try:
                                from .vectorstore import (
                                    collection_doc_count, probe_collection,
                                )
                                item["doc_count"] = collection_doc_count(
                                    client, f["collection"]
                                )
                                item["probe"] = probe_collection(
                                    client, f["collection"], EMBED_DIM
                                )
                            except Exception as e:                # noqa: BLE001
                                item["probe"] = f"error: {type(e).__name__}"
                        entry["files"].append(item)
                    report["matters"].append(entry)
            finally:
                conn.close()
    except Exception as e:                                        # noqa: BLE001
        report["errors"].append(f"manifest: {type(e).__name__}: {e}")

    # --- headline counts, so the first line of the file is the useful one ---
    all_files = [f for m in report["matters"] for f in m["files"]]
    report["summary"] = {
        "matters": len(report["matters"]),
        "files": len(all_files),
        "by_status": {
            s: sum(1 for f in all_files if f["status"] == s)
            for s in sorted({f["status"] for f in all_files})
        },
        "by_probe": {
            s: sum(1 for f in all_files if f.get("probe") == s)
            for s in sorted({f.get("probe") for f in all_files if f.get("probe")})
        },
    }
    return report


SUPPORT_README = """Matter Clerk support report
================================

WHAT THIS IS
    The file named matter-clerk-diagnostic-<date>-<time>.json beside this
    README describes the structure of your Matter Clerk installation. It was
    created when you clicked "Generate support report".

WHAT TO DO
    Email the .json file to your Matter Clerk contact, along with a sentence
    about what you were doing when the problem happened. That sentence is
    genuinely useful -- the report says what the state IS, not what you were
    trying to do.

WHAT IT CONTAINS
    - The version of Matter Clerk you are running
    - How many matters you have, and how many files in each
    - Each file's status, and whether its search index is healthy
    - Whether the database and search index open correctly

WHAT IT DOES NOT CONTAIN
    - Any text from any document
    - Any file name, matter name, or client name
    - Any API key or password

    File names appear only as their type (".pdf") and their length. The report
    is designed so that you do not have to read it before sending it.

    You may open it in Notepad if you would like to check.
"""


def write_diagnostic_report() -> Path:
    """Write the report, plus a plain-English README beside it.

    The README exists because the report on its own leaves a non-technical user
    holding a JSON file with no idea whether it is safe to send or who to send
    it to -- which is how a support tool ends up never being used.
    """
    report = build_diagnostic_report()
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = data_dir() / f"matter-clerk-diagnostic-{stamp}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    try:
        (path.parent / "READ ME - what to do with the support report.txt").write_text(
            SUPPORT_README, encoding="utf-8"
        )
    except Exception as e:                                        # noqa: BLE001
        log.warning(f"could not write support README: {e}")
    return path


# --------------------------------------------------------------------------
# UI preferences (Session 7)
#
# Per-matter file sort order. A JSON file in the data directory rather than a
# `matters` column: this is a display preference, not matter data, and it is
# not worth a schema change plus a migration on every installed copy. It is
# also disposable -- losing it costs the user one dropdown click.
# --------------------------------------------------------------------------
def _prefs_path() -> Path:
    return data_dir() / "ui_prefs.json"


def _load_prefs() -> dict:
    try:
        p = _prefs_path()
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:                                             # noqa: BLE001
        pass
    return {}


def get_matter_sort(matter_id: int, default: str = "oldest") -> str:
    return str(_load_prefs().get("matter_sort", {}).get(str(matter_id), default))


def set_matter_sort(matter_id: int, order: str) -> None:
    """Remember a matter's sort order. Never raises -- a preference that fails
    to save is a dropdown the user clicks again, not an error worth showing."""
    try:
        prefs = _load_prefs()
        prefs.setdefault("matter_sort", {})[str(matter_id)] = order
        p = _prefs_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(prefs, indent=2), encoding="utf-8")
    except Exception as e:                                        # noqa: BLE001
        log.warning(f"could not save sort preference: {e}")


# --------------------------------------------------------------------------
# Permanent deletion of expired soft-deleted items (Session 10)
#
# Runs on a BACKGROUND THREAD after the server is already listening, reusing
# the pattern updater.start_background_check() established. Startup cost is
# therefore zero rather than merely small -- a lawyer with a large backlog of
# deletions should not wait on housekeeping to reach their matters.
#
# Bounded per launch so a big backlog is cleared over several launches instead
# of one long pass, and wrapped so a failure can never affect startup.
# --------------------------------------------------------------------------
PURGE_BATCH = 25


def purge_expired(limit: int = PURGE_BATCH) -> dict:
    """Remove items whose 30-day recovery window has passed."""
    from . import matters
    from .vectorstore import connect, delete_collection

    result = {"files": 0, "matters": 0, "errors": []}
    db = matters.db_path()
    if not db.is_file():
        return result

    conn = matters.connect()
    try:
        files, matter_rows = matters.due_for_purge(conn, limit=limit)

        client = None
        if files or matter_rows:
            try:
                client = connect()
            except Exception as e:                                # noqa: BLE001
                # No store, no collection cleanup. Leave the rows alone rather
                # than dropping the manifest and orphaning the collections --
                # the same reasoning as the Session 6a migration.
                result["errors"].append(f"vector store unavailable: {type(e).__name__}")
                return result

        for row in files:
            try:
                if row.get("collection"):
                    delete_collection(client, row["collection"])
            except Exception as e:                                # noqa: BLE001
                result["errors"].append(f"collection {row.get('collection')}: {type(e).__name__}")
            _unlink_quietly(row.get("stored_path"))
            matters.hard_delete_file(conn, row["id"])
            result["files"] += 1
            log_purge("file", row.get("filename"), row.get("matter_id"))

        for row in matter_rows:
            for f in conn.execute(
                "SELECT * FROM files WHERE matter_id = ?", (row["id"],)
            ).fetchall():
                try:
                    if f["collection"]:
                        delete_collection(client, f["collection"])
                except Exception as e:                            # noqa: BLE001
                    result["errors"].append(f"collection {f['collection']}: {type(e).__name__}")
                _unlink_quietly(f["stored_path"])
            matters.hard_delete_matter(conn, row["id"])
            result["matters"] += 1
            log_purge("matter", row.get("name"), row["id"])
    finally:
        conn.close()
    return result


def _unlink_quietly(path: str | None) -> None:
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except Exception as e:                                        # noqa: BLE001
        log.warning(f"could not remove {path}: {type(e).__name__}")


def log_purge(kind: str, name: str | None, matter_id) -> None:
    from . import audit

    try:
        audit.log_event("permanent_delete", kind=kind, name=name,
                        matter_id=matter_id,
                        after_days=30)
    except Exception:                                             # noqa: BLE001
        pass


def start_purge_in_background() -> None:
    """Kick off the purge on a daemon thread. Returns immediately."""
    import threading

    def worker() -> None:
        try:
            r = purge_expired()
            if r["files"] or r["matters"]:
                log.info(
                    f"permanently removed {r['files']} file(s) and "
                    f"{r['matters']} matter(s) past the 30-day window"
                )
            for err in r["errors"]:
                log.warning(f"purge: {err}")
        except Exception as e:                                    # noqa: BLE001
            log.warning(f"purge failed, continuing: {type(e).__name__}: {e}")

    threading.Thread(target=worker, daemon=True, name="purge-expired").start()
