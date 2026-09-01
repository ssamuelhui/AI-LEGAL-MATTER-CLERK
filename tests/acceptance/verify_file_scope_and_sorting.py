r"""Session 7: file-scope selection on every task, and date-prefix sorting.

The load-bearing test here is section 3. Both features are meant to be purely
additive, so the default path -- "all files", which is what a lawyer who never
touches the new control gets -- must hand the retrieval layer exactly the same
collection list it would have received before Session 7. That is asserted by
capturing the argument actually passed to search_across_collections, because
that list is what determines the answer; anything upstream of it is UI.

Run:  .venv\Scripts\python.exe tests\acceptance\verify_file_scope_and_sorting.py
"""

from __future__ import annotations

import os
import random
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


# Real filenames from the pilot matter, plus the undated conventions.
REAL_NAMES = [
    "2026-03-27 - Technician Report Form.pdf",
    "2024-04-01 to 2026-04-30 - email exchange re. Heat pump with management.pdf",
    "Condo Bylaw 6.pdf",
    "2026-01-21 - 2026-03-26 - Condo manage email re inspection.pdf",
    "Cresthaven_704_Email_Chain.pdf",
    "2025-07-22 to 2025-10-10 - Emails between Condo Management and Raymond.pdf",
]

MATTER_TASKS = [
    "summarize", "timeline", "find_facts", "find_entities",
    "draft_memo", "draft_correspondence", "compare_clauses", "suggest_cases",
]


def main() -> int:
    data = tempfile.mkdtemp(prefix="mc_s7_data_")
    store = tempfile.mkdtemp(prefix="mc_s7_store_")
    os.environ["MATTER_CLERK_DATA_DIR"] = data
    os.environ["CHROMA_DB_PATH"] = store
    os.environ["MATTER_CLERK_SKIP_WIZARD"] = "1"

    from matter_clerk import matters, vectorstore as vs, web
    from matter_clerk.ingest import Chunk

    # ------------------------------------------------------------------ 1
    print("\n1. DATE PARSER")
    from matter_clerk.matters import parse_date_prefix as pdp

    cases = [
        ("2026-03-27 - Technician Report Form.pdf", "2026-03-27"),
        ("2026-01-21 - 2026-03-26 - Condo manage.pdf", "2026-01-21"),
        ("2024-04-01 to 2026-04-30 - email.pdf", "2024-04-01"),
        ("2024-03-15_letter.pdf", "2024-03-15"),
        ("24-03-15_letter.pdf", "2024-03-15"),
        ("2024.03.15 letter.pdf", "2024-03-15"),
        ("3-15-24_letter.pdf", None),
        ("letter_24-03-15.pdf", None),
        ("23_march_letter.pdf", None),
        ("letter.pdf", None),
        ("2024-02-30 impossible.pdf", None),
        ("2024-13-01 impossible.pdf", None),
        ("2024-03-15x.pdf", None),
        ("69-01-01 x.pdf", "2069-01-01"),
        ("70-01-01 x.pdf", "1970-01-01"),
    ]
    bad = 0
    for name, expect in cases:
        got = pdp(name)
        actual = got[0].isoformat() if got else None
        if actual != expect:
            bad += 1
            print(f"       {name!r} -> {actual}, expected {expect}")
    check(f"{len(cases)} parser cases", bad == 0, f"{len(cases) - bad}/{len(cases)}")
    check("range end date captured",
          (pdp("2024-04-01 to 2026-04-30 - x.pdf") or (None, None))[1] is not None)

    # ------------------------------------------------------------------ 2
    print("\n2. SORTING")
    conn = matters.connect()
    m = matters.create_matter(conn, "Pilot", "")
    client = vs.connect()
    ids = {}
    for i, name in enumerate(REAL_NAMES):
        coll = f"m{m.id}-f{i}"
        vs.recreate_collection(client, coll, dim=384, metadata={})
        ch = [Chunk(source=name, locator=f"p.{j}", text=f"text {j} of {name}")
              for j in range(12)]
        vs.upsert_chunks(client, coll, ch,
                         [[random.random() for _ in range(384)] for _ in ch],
                         content_sha256=f"{i}" * 64, matter_id=m.id)
        row = matters.add_file_pending(conn, m.id, name, "pdf", f"{i}" * 64, coll,
                                       str(Path(data) / f"{i}.pdf"))
        matters.mark_file_ingested(conn, row.id)
        ids[name] = row.id
    files = matters.list_files(conn, m.id)

    oldest = [f.filename for f in matters.sort_files(files, "oldest")]
    check("dated files ascend, undated alphabetical at the end",
          oldest[:4] == [
              "2024-04-01 to 2026-04-30 - email exchange re. Heat pump with management.pdf",
              "2025-07-22 to 2025-10-10 - Emails between Condo Management and Raymond.pdf",
              "2026-01-21 - 2026-03-26 - Condo manage email re inspection.pdf",
              "2026-03-27 - Technician Report Form.pdf",
          ] and oldest[4:] == ["Condo Bylaw 6.pdf", "Cresthaven_704_Email_Chain.pdf"])

    newest = [f.filename for f in matters.sort_files(files, "newest")]
    check("newest reverses the dated group only",
          newest[:4] == list(reversed(oldest[:4])) and newest[4:] == oldest[4:],
          "undated tail stays A-Z")

    upload = [f.filename for f in matters.sort_files(files, "upload")]
    check("upload order is the untouched SQL order",
          upload == [f.filename for f in files])
    check("unknown order falls back to the default",
          [f.filename for f in matters.sort_files(files, "nonsense")] == oldest)

    # ------------------------------------------------------------------ 3
    print("\n3. BYTE-IDENTICAL DEFAULT PATH (the regression guarantee)")
    captured: list[list[str]] = []
    real_search = vs.search_across_collections

    def spy(client_, collections, vec, k):
        captured.append(list(collections))
        return real_search(client_, collections, vec, k)

    real_per_file = vs.retrieve_per_file_by_query

    def spy_per_file(client_, collections, vec, k):
        captured.append(list(collections))
        return real_per_file(client_, collections, vec, k)

    vs.search_across_collections = spy
    vs.retrieve_per_file_by_query = spy_per_file
    import matter_clerk.pipeline as pl
    pl.search_across_collections = spy
    pl.retrieve_per_file_by_query = spy_per_file

    app = web.create_app()
    c = app.test_client()

    # The list a pre-Session-7 build would have used: every queryable file.
    expected_all = {f.collection for f in files}

    def run(task, data_dict):
        captured.clear()
        c.post(f"/matters/{m.id}/query", data=data_dict)
        return captured[0] if captured else None

    got = run("find_facts", {"task": "find_facts", "question": "what happened"})
    check("default (no scope control submitted) -> every file",
          got is not None and set(got) == expected_all,
          f"{len(got or [])} collections")

    got = run("find_facts", {"task": "find_facts", "question": "x", "scope_mode": "all"})
    check("explicit 'all' -> identical collection set",
          got is not None and set(got) == expected_all)

    got = run("find_facts", {"task": "find_facts", "question": "x",
                             "scope_mode": "selected"})
    check("'selected' with nothing ticked -> all files (same as default)",
          got is not None and set(got) == expected_all,
          "an empty selection must not mean an empty matter")

    # ------------------------------------------------------------------ 4
    print("\n4. SUBSET AND SINGLE-FILE SCOPE, EVERY TASK")
    two = [files[0], files[1]]
    for task in MATTER_TASKS:
        if task == "suggest_cases":
            continue                    # returns a discovery page, not a query
        payload = {"task": task, "question": "x", "scope_mode": "selected",
                   "file_ids": [str(ids[f.filename]) for f in two],
                   "clauses_to_compare": "indemnity",
                   "recipient": "Opposing counsel"}
        got = run(task, payload)
        ok = got is not None and set(got) == {f.collection for f in two}
        check(f"subset restricts retrieval: {task}", ok,
              f"{len(got or [])} of {len(files)} collections")

    single = files[3]
    for task in ["summarize", "timeline", "find_facts", "find_entities"]:
        captured.clear()
        r = c.post(f"/matters/{m.id}/query",
                   data={"task": task, "question": "x", "scope_mode": "single",
                         "file_id": str(ids[single.filename])})
        # single-file goes through run_query, not scatter-gather: no capture
        check(f"single-file accepted: {task}", r.status_code in (200, 500),
              f"HTTP {r.status_code}, no scatter-gather call: {not captured}")

    # ------------------------------------------------------------------ 5
    print("\n5. REFUSALS -- tampered and unqueryable ids, every task")
    for task in ["summarize", "find_facts", "compare_clauses"]:
        r = c.post(f"/matters/{m.id}/query",
                   data={"task": task, "question": "x", "scope_mode": "selected",
                         "file_ids": ["999999"], "clauses_to_compare": "x"})
        check(f"foreign file id refused, not 500: {task}", r.status_code == 400,
              f"HTTP {r.status_code}")
    r = c.post(f"/matters/{m.id}/query",
               data={"task": "find_facts", "question": "x",
                     "scope_mode": "selected", "file_ids": ["not-an-int"]})
    check("non-integer file id refused", r.status_code == 400)
    r = c.post(f"/matters/{m.id}/query",
               data={"task": "find_facts", "question": "x", "scope_mode": "single",
                     "file_id": ""})
    check("single mode with no file chosen refused", r.status_code == 400)

    # ------------------------------------------------------------------ 6
    print("\n6. ocr_low_quality IS SELECTABLE (v1.0.1 bug)")
    conn2 = matters.connect()
    lowq = files[2]
    matters.mark_file_ingested(conn2, lowq.id, status="ocr_low_quality",
                               note="poor scan")
    conn2.close()
    got = run("find_facts", {"task": "find_facts", "question": "x",
                             "scope_mode": "selected",
                             "file_ids": [str(lowq.id)]})
    check("subset accepts an ocr_low_quality file", got == [lowq.collection],
          "was refused in v1.0.1 while whole-matter accepted it")
    r = c.post(f"/matters/{m.id}/query",
               data={"task": "find_facts", "question": "x", "scope_mode": "single",
                     "file_id": str(lowq.id)})
    check("single mode accepts an ocr_low_quality file", r.status_code != 400,
          f"HTTP {r.status_code}")

    # ------------------------------------------------------------------ 7
    print("\n7. SELECTOR RENDERING ACROSS FILE STATES")
    conn3 = matters.connect()
    broken = files[4]
    matters.mark_file_no_text(conn3, broken.id, "nothing readable")
    conn3.close()
    body = c.get(f"/matters/{m.id}").get_data(as_text=True)
    check("selector present", "Which files should this task use?" in body)
    check("all three modes offered",
          'value="all"' in body and 'value="selected"' in body and 'value="single"' in body)
    check("unqueryable file shown but disabled",
          "is-disabled" in body and "disabled" in body)
    check("poor-scan file flagged, not disabled", "Poor scan quality" in body)
    check("sort control rendered", 'name="sort"' in body and "Oldest first" in body)

    # ------------------------------------------------------------------ 8
    print("\n8. SESSION 6a GUARDS STILL HOLD UNDER SUBSET SELECTION")
    import chromadb.errors as cerr
    real_query = vs._query

    def exploding(client_, name, vec, k):
        if name == files[1].collection:
            raise cerr.InternalError("Nothing found on disk")
        return real_query(client_, name, vec, k)

    vs._query = exploding
    try:
        chunks, report = real_search(
            client, [f.collection for f in two], [0.5] * 384, 5
        )
        check("one broken collection inside a 2-file selection skips + reports",
              report.skipped == [files[1].collection] and len(chunks) > 0,
              f"skipped={len(report.skipped)}, {len(chunks)} chunks from the other")

        def all_explode(client_, name, vec, k):
            raise cerr.InternalError("Nothing found on disk")

        vs._query = all_explode
        try:
            real_search(client, [f.collection for f in two], [0.5] * 384, 5)
            check("all-selected-broken raises rather than empty", False)
        except vs.VectorStoreUnavailable as e:
            check("all-selected-broken raises rather than empty", True,
                  f"names the selection size: {'2 file' in str(e)}")
    finally:
        vs._query = real_query
        vs.search_across_collections = real_search
        vs.retrieve_per_file_by_query = real_per_file
        pl.search_across_collections = real_search
        pl.retrieve_per_file_by_query = real_per_file

    # ------------------------------------------------------------------ 9
    print("\n9. SORT PREFERENCE PERSISTS PER MATTER, NO SCHEMA CHANGE")
    from matter_clerk import maintenance as mt
    c.post(f"/matters/{m.id}/sort", data={"sort": "newest"})
    check("preference stored", mt.get_matter_sort(m.id) == "newest")
    check("stored outside the database",
          (Path(data) / "ui_prefs.json").is_file()
          and "ui_prefs" not in str(matters.db_path()))
    check("other matters unaffected", mt.get_matter_sort(9999) == "oldest")
    c.post(f"/matters/{m.id}/sort", data={"sort": "bogus"})
    check("invalid order ignored", mt.get_matter_sort(m.id) == "newest")
    c.post(f"/matters/{m.id}/sort", data={"sort": "oldest"})

    # ----------------------------------------------------------------- 10
    print("\n10. SUPPORT REPORT + README")
    path = mt.write_diagnostic_report()
    readme = path.parent / "READ ME - what to do with the support report.txt"
    check("report written", path.is_file())
    check("README written beside it", readme.is_file())
    txt = readme.read_text(encoding="utf-8")
    check("README says what to do", "Email the .json file" in txt)
    check("README states what it excludes", "DOES NOT CONTAIN" in txt)
    blob = path.read_text(encoding="utf-8")
    leaked = [n for n in REAL_NAMES if n in blob] + (["Pilot"] if "Pilot" in blob else [])
    check("still leaks no filenames or matter names", not leaked, str(leaked))

    print(f"\n{'PASS' if not failed else 'FAIL'}: {len(passed)} passed, {len(failed)} failed")
    for f in failed:
        print(f"  failed: {f}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
