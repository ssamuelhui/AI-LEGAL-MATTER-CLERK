"""Vector storage and retrieval.

Phase 3: this module was ported from Qdrant to **ChromaDB in embedded mode**.
The reason is deployment, not capability — Qdrant needs a Docker daemon, and the
tool is being installed on Ontario lawyers' Windows laptops where Docker is
frequently prohibited by IT policy, breaks on update, and adds ~500MB the user
has to manage. ChromaDB's PersistentClient is an in-process store over a local
directory: no daemon, no ports, nothing to start before querying.

WHAT THIS MODULE GUARANTEES TO ITS CALLERS
------------------------------------------
`ScoredChunk.score` is a cosine SIMILARITY in [0, 1] where **higher is more
similar**, exactly as it was under Qdrant. This is the single most important
invariant here and it is not free: Qdrant's cosine distance returns a
similarity, whereas Chroma returns a cosine *distance* where LOWER is better.
Porting the ranking code literally would have silently inverted every result —
retrieval would still "work", no error would be raised anywhere, and the tool
would hand the model the least relevant passages in the matter. `_to_similarity`
converts at the boundary so that every caller, every sort, and every score
threshold above this module keeps the meaning it already had.

Cosine is also set EXPLICITLY (`configuration={"hnsw": {"space": "cosine"}}`);
Chroma's default space is L2. Because `embed()` normalises vectors, L2 and
cosine happen to rank identically today — but that is a coincidence of the
current embedding model, not a property we should depend on.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import chromadb
from chromadb.api import ClientAPI

from .ingest import Chunk

log = logging.getLogger("matter_clerk.vectorstore")
from .paths import data_path

# Chroma's HNSW index is configured per collection at creation time. Kept here
# rather than inline so every collection this module creates is provably
# configured the same way.
_COLLECTION_CONFIG = {"hnsw": {"space": "cosine"}}

# Chroma requires collection names of 3-512 chars from [a-zA-Z0-9._-], starting
# and ending alphanumeric. Both of our schemes satisfy this by construction
# ("day1-<sha16>" = 21 chars, "m<id>-<sha16>" >= 19), so the naming scheme
# carried over from Qdrant unchanged — which is what makes an existing
# collection name still mean the same thing after the port.
_MIN_NAME_LEN = 3


@dataclass
class ScoredChunk:
    """One retrieved chunk plus the collection it came from.

    Scatter-gather (Day 4b) merges hits from several per-file collections, so a
    hit must remember its origin collection — that's how the caller maps it back
    to a matter file_id (for the audit log and the "Drew on" label). `source` is
    the human filename from the payload and is what already drives citations;
    `collection` is the machine identity.

    `score` is a cosine similarity, higher-is-better (see module docstring)."""

    collection: str
    score: float
    source: str
    locator: str
    text: str


class VectorStoreUnavailable(RuntimeError):
    """The local vector store could not be opened.

    Replaces Phase-1's QdrantUnreachable. The failure modes are entirely
    different now — not "the daemon is down" but "this path is not writable",
    "the directory is corrupt", or "another process holds it" — so the callers
    that report this to a lawyer need to say something a lawyer can act on."""


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def default_collection_name(pdf_path: Path) -> str:
    return f"day1-{file_hash(pdf_path)[:16]}"


def chunk_id(content_sha256: str, chunk_index: int, text: str) -> str:
    """Deterministic id for one chunk.

    Derived from (file content hash, position, leading text) so that
    re-ingesting identical content produces identical ids. That makes a re-index
    idempotent rather than duplicative, and means an id is reproducible across
    machines — useful when a lawyer reports "the memo cited p.4" and we need to
    find the exact stored chunk.

    `text[:200]` rather than the whole chunk: enough to distinguish two chunks
    that share a position after a chunker change, cheap on very large chunks."""
    h = hashlib.sha256()
    h.update(content_sha256.encode("utf-8"))
    h.update(str(chunk_index).encode("utf-8"))
    h.update(text[:200].encode("utf-8"))
    return h.hexdigest()


# --------------------------------------------------------------------------
# Client lifecycle
#
# One process-wide PersistentClient, created lazily and cached. Chroma is an
# EMBEDDED store: the client owns the directory rather than talking to a server,
# so there is nothing to close and no connection to drop — the SQLite metadata
# and the HNSW index are flushed on write. `connect()` is therefore cheap to
# call repeatedly, which matters because the pipeline calls it once per request.
#
# The trade this makes, and it is a real one: Qdrant was a server, so the web
# app and the CLI could both talk to it at the same time. An embedded store is
# owned by ONE process. Multiple THREADS are fine (the Flask server is threaded
# and Chroma serialises internally), but running the CLI against the same path
# while the web app is up is not supported. Documented in MIGRATION.md.
# --------------------------------------------------------------------------
_CLIENT: ClientAPI | None = None
_CLIENT_PATH: str | None = None


def default_store_path() -> Path:
    r"""Where the vector store lives: $CHROMA_DB_PATH, else <data_dir>/data/chroma.

    <data_dir> is the repo root in a source checkout and
    %LOCALAPPDATA%\MatterClerk in a bundle -- see paths.data_dir()."""
    env = os.environ.get("CHROMA_DB_PATH")
    if env:
        return Path(env)
    return data_path("data/chroma")


def connect(path: str | Path | None = None) -> ClientAPI:
    """Open (or reuse) the embedded store.

    Signature change from Phase 1's `connect(host, port)`: an embedded store has
    no host and no port. Callers pass nothing and get the configured path."""
    global _CLIENT, _CLIENT_PATH
    target = str(Path(path) if path is not None else default_store_path())
    if _CLIENT is not None and _CLIENT_PATH == target:
        return _CLIENT
    try:
        Path(target).mkdir(parents=True, exist_ok=True)
        _CLIENT = chromadb.PersistentClient(path=target)
        _CLIENT_PATH = target
    except Exception as e:
        raise VectorStoreUnavailable(f"{target}: {e}") from e
    return _CLIENT


def reset_client() -> None:
    """Drop the cached client. For tests that switch store paths."""
    global _CLIENT, _CLIENT_PATH
    _CLIENT = None
    _CLIENT_PATH = None


def store_ok(path: str | Path | None = None) -> tuple[bool, str | None]:
    """Health check for the UI: can we open the store and list collections?

    Replaces Phase-1's `_qdrant_ok`. Note that Chroma's client has
    `list_collections()` and NOT `get_collections()` — the old probe would have
    raised AttributeError here and been reported to the lawyer as a dead
    database, so this could not simply be left pointing at the new client."""
    try:
        client = connect(path)
        client.list_collections()
        return True, None
    except Exception as e:
        return False, str(e)


# --------------------------------------------------------------------------
# Collections
# --------------------------------------------------------------------------
def _get(client: ClientAPI, name: str):
    """Fetch a collection, or None if it does not exist.

    `embedding_function=None` on every access: we always supply our own vectors
    (see `upsert_chunks`), and leaving it unset makes Chroma attach its
    DefaultEmbeddingFunction, which downloads an ONNX all-MiniLM-L6-v2 model on
    first use. For an offline Windows installer that is both a surprise network
    fetch and a second, unused embedding model in the bundle."""
    try:
        return client.get_collection(name, embedding_function=None)
    except Exception:
        return None


def collection_exists(client: ClientAPI, name: str) -> bool:
    return _get(client, name) is not None


def recreate_collection(
    client: ClientAPI,
    name: str,
    dim: int,
    metadata: dict | None = None,
) -> None:
    """Drop and recreate `name` as an empty cosine collection.

    `dim` is no longer declared to the store — Chroma infers dimensionality from
    the first vectors written — but it is still accepted and recorded in the
    collection metadata, so a later embed-model swap that changes the dimension
    is visible rather than silent."""
    if len(name) < _MIN_NAME_LEN:
        raise ValueError(
            f"collection name {name!r} is shorter than Chroma's {_MIN_NAME_LEN}-"
            f"character minimum"
        )
    if _get(client, name) is not None:
        client.delete_collection(name)
    meta = {"dim": int(dim), **(metadata or {})}
    # Chroma metadata values must be str/int/float/bool — drop anything else
    # (notably None, which is how an ad-hoc file's absent matter_id arrives).
    meta = {k: v for k, v in meta.items() if isinstance(v, (str, int, float, bool))}
    client.create_collection(
        name=name,
        embedding_function=None,
        configuration=_COLLECTION_CONFIG,
        metadata=meta,
    )


def delete_collection(client: ClientAPI, name: str) -> bool:
    """Delete a collection. Returns False if it did not exist.

    Not called anywhere yet — deleting a matter file currently orphans its
    collection, which is a pre-existing gap unrelated to this port. Exposed here
    so that gap can be closed without reopening the store layer."""
    if _get(client, name) is None:
        return False
    client.delete_collection(name)
    return True


def upsert_chunks(
    client: ClientAPI,
    name: str,
    chunks: list[Chunk],
    vectors: list[list[float]],
    content_sha256: str = "",
    matter_id: int | None = None,
) -> None:
    """Write chunks + their vectors.

    Payload mapping from Qdrant to Chroma: the chunk TEXT becomes Chroma's
    `documents`, and `source` / `locator` become `metadatas`. Same three fields,
    same values, verbatim.

    `locator` in particular is stored and returned untouched. It is the field
    that carries citation semantics ("p.3", "p.5 (OCR)", "from Kevin Oskoui,
    2024-05-11"), and page number and OCR status are encoded IN it rather than
    stored as separate columns — deliberately, so there is exactly one source of
    truth for what a citation says. Splitting them out here would mean parsing a
    format this layer should not know about, and would not survive emails, whose
    locators carry no page at all."""
    if not chunks:
        return
    col = _get(client, name)
    if col is None:
        raise VectorStoreUnavailable(f"collection {name!r} does not exist")
    ids = [chunk_id(content_sha256, i, c.text) for i, c in enumerate(chunks)]
    metadatas: list[dict] = []
    for i, c in enumerate(chunks):
        m: dict = {
            "source": c.source,
            "locator": c.locator,
            "chunk_index": i,
            "content_sha256": content_sha256,
        }
        if matter_id is not None:
            m["matter_id"] = int(matter_id)
        metadatas.append(m)
    col.add(
        ids=ids,
        embeddings=vectors,
        documents=[c.text for c in chunks],
        metadatas=metadatas,
    )


# --------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------
def _to_similarity(distance: float) -> float:
    """Chroma cosine distance -> Qdrant-equivalent cosine similarity.

    Verified against Chroma 1.5.9: an identical vector returns distance 0.0 and
    an orthogonal one returns 1.0, so `1 - d` yields 1.0 and 0.0 respectively —
    exactly the values Qdrant's COSINE distance returned. See the module
    docstring for why getting this backwards would have failed silently."""
    return 1.0 - float(distance)


def _rows(name: str, result: dict) -> list[ScoredChunk]:
    """Flatten one Chroma query result into ScoredChunks.

    Chroma returns column-oriented lists nested one level per query vector; we
    always send exactly one, so everything is index [0]. A collection that holds
    fewer than n_results chunks simply returns fewer — no padding, no error."""
    ids = (result.get("ids") or [[]])[0]
    docs = (result.get("documents") or [[]])[0]
    metas = (result.get("metadatas") or [[]])[0]
    dists = (result.get("distances") or [[]])[0]
    out: list[ScoredChunk] = []
    for i in range(len(ids)):
        meta = metas[i] or {}
        out.append(
            ScoredChunk(
                collection=name,
                score=_to_similarity(dists[i]),
                source=str(meta.get("source", "")),
                locator=str(meta.get("locator", "")),
                text=docs[i] or "",
            )
        )
    return out


def _query(client: ClientAPI, name: str, query_vec: list[float], top_k: int):
    col = _get(client, name)
    if col is None:
        raise VectorStoreUnavailable(f"collection {name!r} does not exist")
    return col.query(
        query_embeddings=[query_vec],
        n_results=top_k,
        include=["documents", "metadatas", "distances"],
    )


def collection_doc_count(client: ClientAPI, name: str) -> int | None:
    """Number of documents in a collection; None if it cannot be read at all.

    Used by ingest verification and by the migration backfill. Deliberately
    interrogates the store rather than trusting an ingest-time counter --
    `IngestOutcome.chunk_count` is 0 for a legitimate cache hit as well as for
    a failed ingest, so it cannot distinguish them.
    """
    try:
        col = _get(client, name)
        if col is None:
            return None
        return int(col.count())
    except Exception:
        return None


def probe_collection(client: ClientAPI, name: str, dim: int) -> str:
    """Classify a collection as 'ok', 'missing', 'empty' or 'unreadable'.

    'unreadable' is the state behind the crash reported from the field: the
    collection is registered and may even report a count, but reading it raises
    from the Rust backend. A count alone does not prove readability, so this
    performs an actual query.
    """
    col = _get(client, name)
    if col is None:
        return "missing"
    try:
        if int(col.count()) == 0:
            return "empty"
        col.query(query_embeddings=[[0.0] * dim], n_results=1, include=[])
        return "ok"
    except Exception:
        return "unreadable"


def _safe(name: str, fn):
    """Run one per-collection read, converting a broken collection into a skip.

    Catches broadly and deliberately. chromadb surfaces corrupt or half-written
    segments as chromadb.errors.InternalError with several different messages
    ("Nothing found on disk", "Missing vector segment"), and the exact set is
    version-dependent -- so narrowing this to known strings would re-open the
    exact failure it exists to prevent. The whole point is that one unreadable
    file must not cost a lawyer the other twenty-seven.

    Returns (value, error_label). Exactly one is None.
    """
    try:
        return fn(), None
    except Exception as e:                                        # noqa: BLE001
        log.warning(
            f"collection {name!r} is unreadable and was skipped: "
            f"{type(e).__name__}: {e}"
        )
        return None, f"{type(e).__name__}: {e}"


@dataclass
class RetrievalReport:
    """Which collections answered, and which could not be read."""

    skipped: list[str]                    # collection names that failed
    attempted: int                        # how many were tried

    @property
    def ok_count(self) -> int:
        return self.attempted - len(self.skipped)


def search(
    client: ClientAPI, name: str, query_vec: list[float], top_k: int
) -> list[ScoredChunk]:
    """Top-k from one collection, best first.

    RETURN TYPE CHANGED in the Chroma port. This used to hand back raw Qdrant
    point objects and the caller reached into `p.payload["source"]` — a vendor
    type leaking two layers up into `pipeline.run_query`. It now returns
    ScoredChunk like its two siblings already did, so no Qdrant-shaped object
    survives anywhere above this module and the next store swap is a one-file
    change for real."""
    return _rows(name, _query(client, name, query_vec, top_k))


def search_across_collections(
    client: ClientAPI,
    collections: list[str],
    query_vec: list[float],
    top_k: int,
) -> tuple[list[ScoredChunk], RetrievalReport]:
    """Scatter-gather retrieval across several collections (Day 4b).

    Retrieves `top_k` from EACH collection independently, then merges by
    similarity score (descending) and returns the global `top_k`. Fetching
    `top_k` per collection is lossless for the global top-k: a chunk in the
    global top-k is necessarily in its own collection's top-k, so no candidate
    that belongs in the final set is missed. Scores are comparable across
    collections because every collection shares the embed model and cosine
    distance. The final list is truncated to `top_k` so the context handed to
    the model is the same size as a single-file query, regardless of how many
    files the matter holds.

    `collections` should be the (already-ingested) per-file collections of the
    matter. An empty list yields an empty result.

    The descending sort is only correct because `_to_similarity` has already
    flipped Chroma's distance into a similarity.

    RETURN TYPE CHANGED in Session 6a: now (chunks, report). A single corrupt
    collection used to raise out of here and cost the caller the entire
    matter; unreadable collections are now skipped and named in the report so
    the result page can say which files did not contribute."""
    merged: list[ScoredChunk] = []
    skipped: list[str] = []
    for name in collections:
        rows, err = _safe(name, lambda n=name: _rows(n, _query(client, n, query_vec, top_k)))
        if err is not None:
            skipped.append(name)
        else:
            merged.extend(rows)

    # All of them failing is a real error, not an empty result. "Nothing matched
    # your question" and "none of your files could be read" must never look the
    # same to a lawyer -- the first is an answer, the second is a broken matter.
    if collections and len(skipped) == len(collections):
        raise VectorStoreUnavailable(
            f"None of the {len(collections)} file(s) in this matter could be "
            "read from the search index. The index may be damaged; run "
            "Diagnostics from the matter page and re-ingest the affected files."
        )

    merged.sort(key=lambda c: c.score, reverse=True)
    return merged[:top_k], RetrievalReport(skipped=skipped, attempted=len(collections))


def retrieve_per_file_by_query(
    client: ClientAPI,
    collections: list[str],
    query_vec: list[float],
    top_k: int,
) -> tuple[dict[str, list[ScoredChunk]], RetrievalReport]:
    """Per-collection retrieval that keeps hits GROUPED by their source (Day 4c).

    Deliberately NOT a mode of `search_across_collections`. That function fetches
    per collection and then merges into one global top-k list; this one fetches
    per collection and stops there. The return type is the difference that
    matters: a dict keyed by collection, not a flat list.

    Two properties Compare Clauses depends on and the merging search cannot
    provide:

      1. The result is TOTAL over `collections`. Every requested collection is a
         key, including one that returned no points, which maps to an empty
         list. A merged top-k silently omits files that lost on score, and
         "this file was searched and yielded nothing" is exactly the fact the
         comparison must be able to state.
      2. Insertion order follows `collections`, so the caller's file order
         becomes the comparison's column order.

    `top_k` is per collection here (not a global cap), so the total number of
    chunks returned is `top_k * len(collections)`; the caller owns that budget.

    Session 6a revised the failure policy, and the original reasoning is worth
    restating because it still holds: a SILENT gap in a legal retrieval is
    worse than a loud failure. An unreadable collection is now skipped rather
    than propagated — but it is named in the returned report and surfaced on
    the result page, so the gap is never silent. What changed is that one
    damaged file no longer costs the lawyer every other file in the matter;
    what did not change is that they are always told.

    Both properties are ours, not Chroma's, and are preserved by construction:
    the dict is built by iterating `collections` in order."""
    out: dict[str, list[ScoredChunk]] = {}
    skipped: list[str] = []
    for name in collections:
        rows, err = _safe(name, lambda n=name: _rows(n, _query(client, n, query_vec, top_k)))
        if err is not None:
            skipped.append(name)
            out[name] = []          # key still present: property 1 above holds
        else:
            out[name] = rows

    if collections and len(skipped) == len(collections):
        raise VectorStoreUnavailable(
            f"None of the {len(collections)} selected file(s) could be read "
            "from the search index."
        )
    return out, RetrievalReport(skipped=skipped, attempted=len(collections))


def all_chunks_for(
    client: ClientAPI, names: list[str], batch: int = 256
) -> tuple[dict[str, list[str]], RetrievalReport]:
    """Whole-collection text for several collections, skipping unreadable ones.

    The guarded sibling of `all_chunks`, for the pleading limitation scan --
    which reads EVERY file in a matter, and so was exposed to exactly the same
    corrupt-collection failure as the query paths. It uses `col.get`, not
    `col.query`, so guarding `_query` alone would have left this path live.

    A skipped collection means the limitation scan did not see that file. That
    is a safety-relevant gap, so the caller must surface it rather than treat
    an incomplete scan as a clean one.
    """
    out: dict[str, list[str]] = {}
    skipped: list[str] = []
    for name in names:
        texts, err = _safe(name, lambda n=name: all_chunks(client, n, batch))
        if err is not None:
            skipped.append(name)
            out[name] = []
        else:
            out[name] = texts
    return out, RetrievalReport(skipped=skipped, attempted=len(names))


def all_chunks(client: ClientAPI, name: str, batch: int = 256) -> list[str]:
    """Return the text of every chunk in a collection (no vectors).

    Used by the pleading limitation scan, which must see the whole document,
    not just the top-k retrieved for the drafting query.

    Chroma's `get` paginates by limit/offset rather than Qdrant's opaque cursor,
    but the batching is kept: the limitation scan runs over whole matters and
    pulling every chunk of every file in one call is the one place this layer
    could plausibly blow up memory on a large matter."""
    col = _get(client, name)
    if col is None:
        raise VectorStoreUnavailable(f"collection {name!r} does not exist")
    texts: list[str] = []
    offset = 0
    while True:
        got = col.get(include=["documents"], limit=batch, offset=offset)
        docs = got.get("documents") or []
        texts.extend(d or "" for d in docs)
        if len(docs) < batch:
            break
        offset += batch
    return texts
