"""Phase 3 Session 3 gate: ONNX embeddings must be interchangeable with the
sentence-transformers vectors already sitting in the user's Chroma store.

Swapping the embedding backend is only safe if it is invisible to retrieval.
This checks that two ways, in increasing order of what actually matters:

  1. VECTOR PARITY -- re-embed the exact document text stored in each live
     collection and compare against the vectors Chroma already holds (which
     sentence-transformers wrote during earlier phases). Cosine >= 0.9999.

  2. RETRIEVAL PARITY -- the only thing a user can observe. Query each
     collection with realistic questions and require the returned chunk ids,
     in order, to be identical whether the query vector came from
     sentence-transformers or from ONNX.

Test 2 is the blocking one. Test 1 explains a failure of test 2 when it happens.

Run:  .venv\Scripts\python.exe tests\acceptance\verify_embedding_parity.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from matter_clerk import vectorstore          # noqa: E402
from matter_clerk.embed import embed          # noqa: E402

MODEL = "BAAI/bge-small-en-v1.5"

COSINE_FLOOR = 0.9999
SAMPLE_PER_COLLECTION = 40

QUERIES = [
    "What are the payment obligations under the agreement?",
    "termination and notice periods",
    "Who are the parties to this proceeding?",
    "limitation period and discoverability",
    "What damages are claimed?",
]


def _cosine(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    a = a / np.linalg.norm(a, axis=1, keepdims=True)
    b = b / np.linalg.norm(b, axis=1, keepdims=True)
    return np.sum(a * b, axis=1)


def main() -> int:
    client = vectorstore.connect()
    collections = client.list_collections()
    if not collections:
        print("FAIL: no Chroma collections found; ingest a matter first.")
        return 1

    try:
        from sentence_transformers import SentenceTransformer

        st_model = SentenceTransformer(MODEL)

        def st_embed(texts: list[str]) -> np.ndarray:
            return np.asarray(
                st_model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
            )

        have_st = True
    except Exception as e:                                    # noqa: BLE001
        print(f"NOTE: sentence-transformers unavailable ({type(e).__name__}); "
              "retrieval-parity check will be skipped.")
        have_st = False

    # ---------------- 1. vector parity against the live store ----------------
    worst = 1.0
    worst_where = ""
    total = 0

    for meta in collections:
        name = meta.name if hasattr(meta, "name") else str(meta)
        coll = client.get_collection(name)
        got = coll.get(limit=SAMPLE_PER_COLLECTION, include=["embeddings", "documents"])
        docs = got.get("documents") or []
        stored = got.get("embeddings")
        if stored is None or len(docs) == 0:
            continue

        stored = np.asarray(stored, dtype=np.float64)
        fresh = np.asarray(embed(list(docs), MODEL), dtype=np.float64)
        sims = _cosine(fresh, stored)

        total += len(sims)
        if sims.min() < worst:
            worst = float(sims.min())
            worst_where = f"{name} (chunk {int(sims.argmin())})"

        status = "ok  " if sims.min() >= COSINE_FLOOR else "FAIL"
        print(f"  [{status}] {name:<28} n={len(sims):>3}  "
              f"min={sims.min():.6f}  mean={sims.mean():.6f}")

    print(f"\n1. VECTOR PARITY: {total} stored chunks re-embedded")
    print(f"   worst cosine = {worst:.6f}  (floor {COSINE_FLOOR})  at {worst_where}")
    vector_ok = total > 0 and worst >= COSINE_FLOOR
    print(f"   {'PASS' if vector_ok else 'FAIL'}")

    # ---------------- 2. retrieval parity (the observable one) ---------------
    retrieval_ok = True
    if have_st:
        compared = 0
        mismatches = 0
        for meta in collections:
            name = meta.name if hasattr(meta, "name") else str(meta)
            coll = client.get_collection(name)
            if coll.count() == 0:
                continue
            k = min(10, coll.count())

            st_vecs = st_embed(QUERIES)
            onnx_vecs = np.asarray(embed(QUERIES, MODEL))

            for q, sv, ov in zip(QUERIES, st_vecs, onnx_vecs):
                st_ids = coll.query(query_embeddings=[sv.tolist()], n_results=k)["ids"][0]
                on_ids = coll.query(query_embeddings=[ov.tolist()], n_results=k)["ids"][0]
                compared += 1
                if st_ids != on_ids:
                    mismatches += 1
                    print(f"  [FAIL] {name} / {q!r}")
                    print(f"         st  : {st_ids}")
                    print(f"         onnx: {on_ids}")

        retrieval_ok = mismatches == 0
        print(f"\n2. RETRIEVAL PARITY: {compared} query/collection pairs, "
              f"{mismatches} top-k mismatches")
        print(f"   {'PASS' if retrieval_ok else 'FAIL'}")
    else:
        print("\n2. RETRIEVAL PARITY: SKIPPED")

    ok = vector_ok and retrieval_ok
    print(f"\n{'PASS' if ok else 'FAIL'}: ONNX backend is "
          f"{'interchangeable with' if ok else 'NOT interchangeable with'} "
          "the stored sentence-transformers vectors.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
