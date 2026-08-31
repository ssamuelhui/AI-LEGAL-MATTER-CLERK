"""Offline verification for Compare Clauses (Day 4c).

Runs the real `pipeline.run_compare_clauses` with the store and the LLM stubbed, so
the logic that matters — per-file grouping, the absent-file path, provenance
covering files that contributed nothing, the retrieval budget, and the file-count
refusals — is checkable without Docker or an API key.

No pytest dependency (the project has none configured):

    python tests/acceptance/verify_compare_clauses.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from matter_clerk import pipeline  # noqa: E402
from matter_clerk.matters import MatterFile  # noqa: E402
from matter_clerk.vectorstore import ScoredChunk  # noqa: E402

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"         got:  {got!r}\n         want: {want!r}")
        FAILURES.append(label)


def check_true(label: str, cond: bool) -> None:
    check(label, bool(cond), True)


def mk_file(i: int, name: str) -> MatterFile:
    return MatterFile(
        id=i, matter_id=1, filename=name, file_type="pdf",
        content_sha256=f"{i:064x}", collection=f"m1-{i:016x}",
        stored_path=f"/tmp/{name}", ingest_status="ingested",
        ingest_error=None, ingested_at="2026-08-06", created_at="2026-08-06",
    )


class FakeLLM:
    """Captures the assembled prompts instead of calling a provider."""

    captured: dict = {}

    def __init__(self, model: str) -> None:
        pass

    def complete(self, messages):
        FakeLLM.captured = {m["role"]: m["content"] for m in messages}
        return "| Attribute | a.pdf |\n| --- | --- |\n| Clause located at | s.8 [a.pdf p.1] |"


def install_stubs(hits_by_collection: dict[str, int]) -> None:
    """Stub the vector store + embeddings + the LLM. `hits_by_collection` says
    how many passages each collection returns; 0 means the file yielded nothing.

    Phase 3: `connect` lost its (host, port) arguments with the move to the
    embedded store, and `_precheck_qdrant` was renamed `_precheck_store`."""
    pipeline.connect = lambda *a, **kw: object()
    pipeline._precheck_store = lambda client: None
    pipeline.embed = lambda texts, model_name=None: [[0.0] * 384 for _ in texts]
    pipeline.LLMClient = FakeLLM

    def fake_retrieve(client, collections, query_vec, top_k):
        out = {}
        for c in collections:
            n = min(hits_by_collection.get(c, 0), top_k)
            out[c] = [
                ScoredChunk(
                    collection=c, score=0.9 - 0.01 * j,
                    source=f"src-{c}", locator=f"p.{j + 1}",
                    text=f"passage {j + 1} from {c}",
                )
                for j in range(n)
            ]
        return out

    pipeline.retrieve_per_file_by_query = fake_retrieve


def main() -> int:
    files = [
        mk_file(1, "Master_Services_Agreement.pdf"),
        mk_file(2, "Cresthaven_Subcontract.pdf"),
        mk_file(3, "Technician_Report.pdf"),   # yields nothing
    ]
    # Every collection has plenty of passages except file 3, which has none.
    install_stubs({files[0].collection: 9, files[1].collection: 9,
                   files[2].collection: 0})

    print("\n1. Retrieval budget (pure function, no I/O)")
    check("2 files -> 6 per file", pipeline.compare_per_file_top_k(6, 2), 6)
    check("6 files -> 6 per file", pipeline.compare_per_file_top_k(6, 6), 6)
    check("10 files -> 4 per file", pipeline.compare_per_file_top_k(6, 10), 4)
    check("20 files -> floor of 3", pipeline.compare_per_file_top_k(6, 20), 3)
    check_true("floor never yields 0", pipeline.compare_per_file_top_k(6, 999) >= 3)

    print("\n2. File-count refusals (never a silent truncation)")
    for n, frag in ((1, "at least 2"), (21, "limited to 20")):
        try:
            pipeline.run_compare_clauses(
                files=[mk_file(i, f"f{i}.pdf") for i in range(1, n + 1)],
                structured_inputs={"clauses_to_compare": "indemnity"},
                matter_id=1,
            )
            check(f"{n} files refused", "no exception", "refusal")
        except pipeline.CompareClausesNotApplicable as e:
            check_true(f"{n} files refused ({frag!r})", frag in str(e))

    print("\n3. Full run with one file that has no relevant passages")
    result = pipeline.run_compare_clauses(
        files=files,
        structured_inputs={"clauses_to_compare": "insurance and coverage"},
        matter_id=1,
    )
    user = FakeLLM.captured["user"]
    system = FakeLLM.captured["system"]

    check("cross_document set", result.cross_document, True)
    check("task id", result.task, "compare_clauses")
    check("effective per-file top_k reported", result.top_k, 6)
    check(
        "provenance lists ALL files checked, in matter order",
        result.retrieved_sources,
        [f.filename for f in files],
    )
    check("provenance file ids likewise", result.retrieved_file_ids, [1, 2, 3])
    check_true(
        "the file with no passages is still in provenance",
        "Technician_Report.pdf" in result.retrieved_sources,
    )
    check(
        "citations come only from files that returned passages",
        len(result.citations), 12,   # 6 + 6 + 0
    )

    print("\n4. The assembled prompt")
    check_true("FILE MANIFEST present", user.startswith("FILE MANIFEST"))
    check_true(
        "empty file marked NO passages in manifest",
        "3. Technician_Report.pdf - NO passages retrieved" in user,
    )
    check_true(
        "non-empty files carry their counts",
        "1. Master_Services_Agreement.pdf - 6 passage(s) retrieved" in user,
    )
    check_true(
        "empty file gets an explicit context block, not silence",
        "=== DOCUMENT: Technician_Report.pdf ===" in user
        and "no passages relevant to the requested clause" in user,
    )
    check_true("manifest order == matter order", user.index(
        "=== DOCUMENT: Master_Services_Agreement.pdf ===") < user.index(
        "=== DOCUMENT: Cresthaven_Subcontract.pdf ===") < user.index(
        "=== DOCUMENT: Technician_Report.pdf ==="))
    check_true("REQUEST carries the clause text",
               "Clauses to compare: insurance and coverage" in user)
    check_true("file_ids (a control input) stays OUT of REQUEST",
               "file_ids" not in user)
    check_true("matter-mode safety preamble applied",
               "on the documents in this matter (the case file)" in system)
    check_true("citation rule 1 intact",
               "copied exactly as written in the passage" in system)
    check_true("rule 10 (no legal commentary) present",
               "Do not judge which" in system)
    check_true("absent-clause wording present",
               "Not present in this document" in system)

    print("\n5. Subset selection narrows the comparison")
    result2 = pipeline.run_compare_clauses(
        files=files[:2],
        structured_inputs={"clauses_to_compare": "insurance"},
        matter_id=1,
    )
    check("subset provenance", result2.retrieved_sources,
          [files[0].filename, files[1].filename])
    check_true("technician report absent from a subset run",
               "Technician_Report.pdf" not in FakeLLM.captured["user"])

    print()
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED: {FAILURES}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
