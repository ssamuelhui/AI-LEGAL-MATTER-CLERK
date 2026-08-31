"""Verification for the Phase-3 ChromaDB vector store.

    python tests/acceptance/verify_vectorstore.py

Runs against a real ChromaDB store in a temp directory -- no Docker, no daemon,
no network, no LLM. Embeddings are hand-built unit vectors rather than real
sentence-transformers output, so the assertions are about STORAGE and RANKING
semantics, which is exactly what the port changed.

The load-bearing test here is `verify_ranking_direction`. Qdrant's cosine
distance returns a SIMILARITY (higher is better); Chroma returns a DISTANCE
(lower is better). Porting the sort literally would have inverted every result
in the tool with no error anywhere -- retrieval would still return chunks, just
the wrong ones. Nothing else in the test suite would have caught that.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from matter_clerk import vectorstore as vs  # noqa: E402
from matter_clerk.ingest import Chunk  # noqa: E402

_passed = 0
_failed: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  [ok]   {label}")
    else:
        _failed.append(f"{label}{(' - ' + detail) if detail else ''}")
        print(f"  [FAIL] {label}{(' - ' + detail) if detail else ''}")


# Three orthogonal unit vectors: each is maximally similar to itself and
# exactly orthogonal to the others, so expected ranking is unambiguous.
E1 = [1.0, 0.0, 0.0]
E2 = [0.0, 1.0, 0.0]
E3 = [0.0, 0.0, 1.0]

# A vector close to E1 but not identical, for a graded-ranking assertion.
NEAR_E1 = [0.94, 0.34, 0.0]


def _chunks(source: str, locators: list[str], texts: list[str]) -> list[Chunk]:
    return [
        Chunk(source=source, locator=loc, text=txt)
        for loc, txt in zip(locators, texts)
    ]


def _seed(client, name: str, chunks: list[Chunk], vectors: list[list[float]],
          sha: str = "a" * 64, matter_id: int | None = None) -> None:
    vs.recreate_collection(client, name, dim=len(vectors[0]))
    vs.upsert_chunks(
        client, name, chunks, vectors, content_sha256=sha, matter_id=matter_id
    )


# --------------------------------------------------------------------------
def verify_roundtrip(client) -> None:
    """Payload fields must survive storage verbatim -- especially `locator`,
    which carries citation semantics (page number and OCR status are encoded in
    it, not stored separately)."""
    print("\nPayload round-trip")
    name = "day1-roundtrip0000"
    locators = ["p.1", "p.5 (OCR)", "from Kevin Oskoui, 2024-05-11"]
    texts = ["alpha text", "beta text", "gamma text"]
    _seed(client, name, _chunks("Lease.pdf", locators, texts), [E1, E2, E3])

    hits = vs.search(client, name, E2, top_k=1)
    check("returns exactly one hit for top_k=1", len(hits) == 1)
    h = hits[0]
    check("source preserved", h.source == "Lease.pdf", h.source)
    check("OCR locator preserved verbatim", h.locator == "p.5 (OCR)", h.locator)
    check("text preserved", h.text == "beta text", h.text)
    check("collection recorded on the hit", h.collection == name, h.collection)

    email = vs.search(client, name, E3, top_k=1)[0]
    check("email locator preserved verbatim",
          email.locator == "from Kevin Oskoui, 2024-05-11", email.locator)


def verify_ranking_direction(client) -> None:
    """THE regression this port could most easily have shipped silently.

    `score` must be a similarity: higher = more similar. If the Chroma distance
    were passed through unconverted, the nearest chunk would score 0.0, the
    furthest 1.0, and every descending sort in the codebase would return the
    least relevant passages."""
    print("\nRanking direction (distance -> similarity)")
    name = "day1-ranking00000"
    _seed(client, name, _chunks("D.pdf", ["p.1", "p.2", "p.3"],
                                ["near", "far", "other"]), [E1, E2, E3])

    hits = vs.search(client, name, E1, top_k=3)
    check("nearest chunk ranks FIRST", hits[0].text == "near",
          f"got {hits[0].text!r} first")
    check("identical vector scores ~1.0", abs(hits[0].score - 1.0) < 1e-6,
          str(hits[0].score))
    check("orthogonal vector scores ~0.0", abs(hits[-1].score - 0.0) < 1e-6,
          str(hits[-1].score))
    check("scores descend", all(hits[i].score >= hits[i + 1].score
                                for i in range(len(hits) - 1)))

    graded = vs.search(client, name, NEAR_E1, top_k=3)
    check("graded ranking: closer chunk beats orthogonal one",
          graded[0].text == "near" and graded[0].score > graded[1].score,
          f"{[(g.text, round(g.score, 3)) for g in graded]}")


def verify_scatter_gather(client) -> None:
    """Day-4b semantics: per-collection fetch, merge by score, global top-k."""
    print("\nScatter-gather across collections")
    a, b = "m1-scatteraaaaaaa", "m1-scatterbbbbbbb"
    _seed(client, a, _chunks("A.pdf", ["p.1", "p.2"], ["a-near", "a-far"]),
          [E1, E2], sha="a" * 64, matter_id=1)
    _seed(client, b, _chunks("B.pdf", ["p.1", "p.2"], ["b-mid", "b-far"]),
          [NEAR_E1, E3], sha="b" * 64, matter_id=1)

    merged = vs.search_across_collections(client, [a, b], E1, top_k=3)
    check("truncated to the global top_k", len(merged) == 3, str(len(merged)))
    check("best hit is the exact match from A", merged[0].text == "a-near",
          merged[0].text)
    check("second is the near hit from B", merged[1].text == "b-mid",
          merged[1].text)
    check("hits remember their origin collection",
          merged[0].collection == a and merged[1].collection == b)
    check("merged scores descend",
          all(merged[i].score >= merged[i + 1].score for i in range(2)))
    check("empty collection list yields nothing",
          vs.search_across_collections(client, [], E1, top_k=3) == [])


def verify_per_file_grouping(client) -> None:
    """Day-4c semantics: the result is TOTAL over `collections` and ordered by
    the caller's list -- including a collection that returns nothing."""
    print("\nPer-file grouped retrieval (Compare Clauses)")
    a, b, empty = "m2-perfileaaaaaa", "m2-perfilebbbbbb", "m2-perfileempty0"
    _seed(client, a, _chunks("A.pdf", ["p.1"], ["a-text"]), [E1],
          sha="a" * 64, matter_id=2)
    _seed(client, b, _chunks("B.pdf", ["p.1"], ["b-text"]), [E2],
          sha="b" * 64, matter_id=2)
    # A collection that exists but holds nothing: "searched and found nothing"
    # is the fact Compare Clauses must be able to state.
    vs.recreate_collection(client, empty, dim=3)

    got = vs.retrieve_per_file_by_query(client, [b, empty, a], E1, top_k=2)
    check("every requested collection is a key", set(got) == {a, b, empty})
    check("insertion order follows the caller's list",
          list(got) == [b, empty, a], str(list(got)))
    check("zero-hit collection maps to an empty list", got[empty] == [],
          str(got[empty]))
    check("populated collections return their chunk",
          got[a][0].text == "a-text" and got[b][0].text == "b-text")


def verify_all_chunks(client) -> None:
    """The limitation scan must see the WHOLE document, not a top-k."""
    print("\nall_chunks (limitation scan)")
    name = "day1-allchunks000"
    n = 27
    chunks = _chunks("Big.pdf", [f"p.{i}" for i in range(n)],
                     [f"chunk-{i}" for i in range(n)])
    vecs = [[1.0, float(i) / 100.0, 0.0] for i in range(n)]
    _seed(client, name, chunks, vecs)

    texts = vs.all_chunks(client, name)
    check("returns every chunk", len(texts) == n, f"{len(texts)} of {n}")
    check("content intact", set(texts) == {f"chunk-{i}" for i in range(n)})

    # Batch smaller than the collection: exercises the pagination loop, which
    # is where an off-by-one silently truncates a limitation scan.
    batched = vs.all_chunks(client, name, batch=5)
    check("batching returns the same set", sorted(batched) == sorted(texts),
          f"{len(batched)} vs {len(texts)}")

    exact = vs.all_chunks(client, name, batch=n)
    check("batch == collection size returns all (no dropped final page)",
          len(exact) == n, f"{len(exact)} of {n}")


def verify_collection_lifecycle(client) -> None:
    print("\nCollection lifecycle")
    name = "day1-lifecycle000"
    check("absent collection reports not-exists",
          not vs.collection_exists(client, name))
    _seed(client, name, _chunks("X.pdf", ["p.1"], ["only"]), [E1])
    check("present after create", vs.collection_exists(client, name))

    # recreate must EMPTY it: a re-index that appended would duplicate chunks.
    _seed(client, name, _chunks("X.pdf", ["p.1"], ["replaced"]), [E1])
    texts = vs.all_chunks(client, name)
    check("recreate empties before rewrite", texts == ["replaced"], str(texts))

    check("delete_collection removes it", vs.delete_collection(client, name))
    check("gone after delete", not vs.collection_exists(client, name))
    check("deleting a missing collection returns False",
          vs.delete_collection(client, name) is False)

    try:
        vs.recreate_collection(client, "ab", dim=3)
    except ValueError:
        check("name shorter than Chroma's 3-char minimum is refused", True)
    else:
        check("name shorter than Chroma's 3-char minimum is refused", False)


def verify_chunk_ids() -> None:
    """Ids are derived from content, so a re-ingest of identical bytes is
    idempotent rather than duplicative."""
    print("\nChunk id derivation")
    a = vs.chunk_id("sha-1", 0, "some text")
    b = vs.chunk_id("sha-1", 0, "some text")
    check("deterministic for identical input", a == b)
    check("differs by chunk index", a != vs.chunk_id("sha-1", 1, "some text"))
    check("differs by file content hash", a != vs.chunk_id("sha-2", 0, "some text"))
    check("differs by text", a != vs.chunk_id("sha-1", 0, "other text"))
    check("is a hex sha256", len(a) == 64 and all(c in "0123456789abcdef" for c in a))
    long_a = vs.chunk_id("sha", 0, "x" * 500 + "TAIL-A")
    long_b = vs.chunk_id("sha", 0, "x" * 500 + "TAIL-B")
    check("only the first 200 chars participate (documented behaviour)",
          long_a == long_b)


def verify_naming() -> None:
    print("\nCollection naming (unchanged from Phase 1)")
    f = Path(__file__)
    name = vs.default_collection_name(f)
    check("ad-hoc scheme is day1-<sha16>",
          name.startswith("day1-") and len(name) == len("day1-") + 16, name)
    check("name satisfies Chroma's charset + length rules",
          len(name) >= 3 and all(c.isalnum() or c in "._-" for c in name)
          and name[0].isalnum() and name[-1].isalnum())
    check("hash is stable for the same bytes",
          vs.default_collection_name(f) == name)


def verify_metadata(client) -> None:
    print("\nCollection metadata")
    name = "m7-metadata00000"
    vs.recreate_collection(client, name, dim=3, metadata={
        "created_at": "2026-08-30T00:00:00+00:00",
        "source_filename": "Lease.pdf",
        "content_sha256": "c" * 64,
        "embed_model": "BAAI/bge-small-en-v1.5",
        "matter_id": 7,
    })
    col = client.get_collection(name, embedding_function=None)
    meta = col.metadata or {}
    check("dim recorded", meta.get("dim") == 3, str(meta.get("dim")))
    check("source filename recorded", meta.get("source_filename") == "Lease.pdf")
    check("content hash recorded", meta.get("content_sha256") == "c" * 64)
    check("embed model recorded",
          meta.get("embed_model") == "BAAI/bge-small-en-v1.5")
    check("matter_id recorded", meta.get("matter_id") == 7)

    # None is not a legal Chroma metadata value; an ad-hoc file has no matter.
    vs.recreate_collection(client, "day1-nomatter0000", dim=3,
                           metadata={"matter_id": None})
    m2 = client.get_collection("day1-nomatter0000",
                               embedding_function=None).metadata or {}
    check("None-valued metadata is dropped, not written",
          "matter_id" not in m2, str(m2))


def verify_cosine_space(client) -> None:
    """Chroma defaults to L2. Normalised vectors make L2 and cosine rank the
    same, so a wrong space would NOT show up in the ranking tests -- it has to
    be asserted directly."""
    print("\nIndex configuration")
    name = "day1-cosinespace"
    vs.recreate_collection(client, name, dim=3)
    col = client.get_collection(name, embedding_function=None)
    space = (col.configuration_json or {}).get("hnsw", {}).get("space")
    check("collection is configured for cosine, not the L2 default",
          space == "cosine", str(space))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mc-vs-"))
    try:
        vs.reset_client()
        client = vs.connect(tmp)
        print(f"Store: {tmp}")
        verify_naming()
        verify_chunk_ids()
        verify_cosine_space(client)
        verify_metadata(client)
        verify_roundtrip(client)
        verify_ranking_direction(client)
        verify_scatter_gather(client)
        verify_per_file_grouping(client)
        verify_all_chunks(client)
        verify_collection_lifecycle(client)
    finally:
        vs.reset_client()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{_passed} passed, {len(_failed)} failed")
    for f in _failed:
        print(f"  - {f}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
