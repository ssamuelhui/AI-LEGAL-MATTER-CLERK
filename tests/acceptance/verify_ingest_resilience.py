r"""Session 6a: the field-reported crash class must not take down a matter.

A pilot lawyer uploaded 28 files and got a bare Internal Server Error from
`chromadb.errors.InternalError: ... Error creating hnsw segment reader: Nothing
found on disk`, raised out of `search_across_collections`.

IMPORTANT, and the reason this file reads the way it does: the reported root
cause -- "SQLite says ingested but the Chroma collection is empty" -- was tested
directly and does NOT reproduce that crash on chromadb 1.5.x. An empty
collection returns an empty result set, cleanly. Eight distinct corruption
shapes were tried; see docs/ARCHITECTURE.md. So these tests do not pretend to
recreate the lawyer's exact state. They verify the property that actually
protects them, which holds whatever produced it:

    ONE unreadable collection must never cost the lawyer the other 27,
    and the loss must never be silent.

Run:  .venv\Scripts\python.exe tests\acceptance\verify_ingest_resilience.py
"""

from __future__ import annotations

import os
import random
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

passed: list[str] = []
failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passed if ok else failed).append(name)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {name}{(' -- ' + detail) if detail else ''}")


def _fill(vs, client, name: str, n: int = 40) -> None:
    from matter_clerk.ingest import Chunk

    vs.recreate_collection(client, name, dim=384,
                           metadata={"source_filename": f"{name}.pdf"})
    chunks = [Chunk(source=f"{name}.pdf", locator=f"p.{i}", text=f"clause {i} rent arrears")
              for i in range(n)]
    vecs = [[random.random() for _ in range(384)] for _ in range(n)]
    vs.upsert_chunks(client, name, chunks, vecs, content_sha256="a" * 64, matter_id=1)


def main() -> int:
    store = tempfile.mkdtemp(prefix="mc_resilience_")
    data = tempfile.mkdtemp(prefix="mc_data_")
    os.environ["CHROMA_DB_PATH"] = store
    os.environ["MATTER_CLERK_DATA_DIR"] = data

    from matter_clerk import maintenance, vectorstore as vs

    client = vs.connect()

    # ---------------------------------------------------------------- 1
    print("\n1. GUARD: one unreadable collection among many")
    good = [f"m1-good{i}" for i in range(3)]
    for g in good:
        _fill(vs, client, g)

    # A collection that raises on read. Monkeypatched rather than corrupted on
    # disk, because the point is the GUARD, and the guard must hold for any
    # exception the store can raise -- not only the shapes reproducible here.
    import chromadb.errors as cerr

    real_query = vs._query

    def exploding(client_, name, vec, k):
        if name == "m1-broken":
            raise cerr.InternalError(
                "Error executing plan: Internal error: Error creating hnsw "
                "segment reader: Nothing found on disk"
            )
        return real_query(client_, name, vec, k)

    vs._query = exploding
    try:
        colls = good + ["m1-broken"]
        chunks, report = vs.search_across_collections(client, colls, [0.5] * 384, 5)
        check("query survives the broken collection", True,
              f"{len(chunks)} chunks from {report.ok_count}/{report.attempted} collections")
        check("broken collection is REPORTED, not silently dropped",
              report.skipped == ["m1-broken"], f"skipped={report.skipped}")
        check("good collections still contribute", len(chunks) > 0)

        # ------------------------------------------------------------ 2
        print("\n2. GUARD: every collection unreadable is an ERROR, not an empty answer")
        def all_explode(client_, name, vec, k):
            raise cerr.InternalError("Nothing found on disk")

        vs._query = all_explode
        try:
            vs.search_across_collections(client, colls, [0.5] * 384, 5)
            check("all-fail raises rather than returning empty", False,
                  "returned normally -- 'no results' would be indistinguishable "
                  "from 'nothing readable'")
        except vs.VectorStoreUnavailable as e:
            check("all-fail raises rather than returning empty", True, str(e)[:60])

        # ------------------------------------------------------------ 3
        print("\n3. GUARD: the other two retrieval primitives")
        vs._query = exploding
        by_coll, rep = vs.retrieve_per_file_by_query(client, colls, [0.5] * 384, 3)
        check("retrieve_per_file_by_query survives", rep.skipped == ["m1-broken"])
        check("  ... and keeps a key for the broken file",
              "m1-broken" in by_coll and by_coll["m1-broken"] == [])

        real_all = vs.all_chunks

        def all_chunks_explode(client_, name, batch=256):
            if name == "m1-broken":
                raise cerr.InternalError("Nothing found on disk")
            return real_all(client_, name, batch)

        vs.all_chunks = all_chunks_explode
        try:
            texts, rep2 = vs.all_chunks_for(client, colls)
            check("all_chunks_for survives (limitation-scan path)",
                  rep2.skipped == ["m1-broken"], f"skipped={rep2.skipped}")
            check("  ... and still returns the good files' text",
                  sum(len(v) for k, v in texts.items() if k != "m1-broken") > 0)
        finally:
            vs.all_chunks = real_all
    finally:
        vs._query = real_query

    # ---------------------------------------------------------------- 4
    print("\n4. INGEST: an empty collection can no longer be inherited")
    vs.recreate_collection(client, "m1-emptied", dim=384, metadata={})
    check("collection_doc_count sees zero", vs.collection_doc_count(client, "m1-emptied") == 0)
    check("probe_collection classifies it 'empty'",
          vs.probe_collection(client, "m1-emptied", 384) == "empty")
    check("probe_collection classifies a healthy one 'ok'",
          vs.probe_collection(client, good[0], 384) == "ok")
    check("probe_collection classifies an absent one 'missing'",
          vs.probe_collection(client, "m1-nonexistent", 384) == "missing")

    # ---------------------------------------------------------------- 5
    print("\n5. MIGRATION: a simulated broken install is healed")
    db = Path(data) / "matter_clerk.db"
    from matter_clerk import matters

    conn = matters.connect()
    m = matters.create_matter(conn, "Pilot", "simulated broken install")
    rows = []
    for idx, (name, coll) in enumerate([("healthy.pdf", good[0]),
                                        ("gone.pdf", "m1-absent"),
                                        ("hollow.pdf", "m1-emptied")]):
        # distinct content hashes: the manifest rejects duplicates per matter
        r = matters.add_file_pending(conn, m.id, name, "pdf", f"{idx}" * 64, coll,
                                     str(Path(data) / name))
        matters.mark_file_ingested(conn, r.id)
        rows.append(r)
    conn.close()

    result = maintenance.backfill_ingest_status()
    check("migration ran without error", result["error"] is None, str(result["error"]))
    check("checked all three files", result["checked"] == 3, f"checked={result['checked']}")
    check("demoted exactly the two broken ones", result["demoted"] == 2,
          f"demoted={result['demoted']} -> {[f['filename'] for f in result['files']]}")

    conn = matters.connect()
    statuses = {f.filename: f.ingest_status for f in matters.list_files(conn, m.id)}
    conn.close()
    check("healthy file left alone", statuses["healthy.pdf"] == "ingested")
    check("missing-collection file demoted", statuses["gone.pdf"] == "failed_no_text")
    check("empty-collection file demoted", statuses["hollow.pdf"] == "failed_no_text")
    check("demoted files are excluded from queries",
          not matters.is_queryable("failed_no_text")
          and matters.is_queryable("ocr_low_quality"))

    # ---------------------------------------------------------------- 6
    print("\n6. MIGRATION: runs once, and never on an unopenable store")
    maintenance.run_startup_migrations()
    marker = Path(data) / "migrations" / f"{maintenance.MIGRATION_BACKFILL_INGEST_STATUS}.done"
    check("marker written", marker.is_file())
    before = marker.read_text(encoding="utf-8")
    maintenance.run_startup_migrations()
    check("second run is a no-op", marker.read_text(encoding="utf-8") == before)

    # An existing FILE, not a directory: PersistentClient cannot create its
    # store there, which is the closest reproducible stand-in for a store that
    # will not open on a user machine.
    os.environ["CHROMA_DB_PATH"] = str(Path(data) / "matter_clerk.db")
    vs._CLIENT = None
    vs._CLIENT_PATH = None
    r2 = maintenance.backfill_ingest_status()
    check("unopenable store is reported, not acted on",
          r2["error"] is not None and r2["demoted"] == 0, str(r2["error"])[:60])
    os.environ["CHROMA_DB_PATH"] = store
    vs._CLIENT = None

    # ---------------------------------------------------------------- 7
    print("\n7. NOTICES: one-shot delivery")
    maintenance.push_notice("warning", "two files need re-upload")
    check("notice queued", len(maintenance.take_notices()) == 1)
    check("notice consumed exactly once", maintenance.take_notices() == [])

    # ---------------------------------------------------------------- 8
    print("\n8. DIAGNOSTIC: useful, and free of client content")
    report = maintenance.build_diagnostic_report()
    blob = str(report)
    check("records app version", report.get("app_version") == "1.0.1")
    check("counts files by status", bool(report["summary"]["by_status"]),
          str(report["summary"]["by_status"]))
    check("NO filenames leak", "healthy.pdf" not in blob and "hollow.pdf" not in blob)
    check("NO matter names leak", "Pilot" not in blob)
    check("NO document text leaks", "rent arrears" not in blob)
    check("carries per-file probe results",
          any("probe" in f for m_ in report["matters"] for f in m_["files"]))

    print(f"\n{'PASS' if not failed else 'FAIL'}: {len(passed)} passed, {len(failed)} failed")
    for f in failed:
        print(f"  failed: {f}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
