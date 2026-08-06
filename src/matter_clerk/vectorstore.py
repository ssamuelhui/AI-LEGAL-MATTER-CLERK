from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from .ingest import Chunk


@dataclass
class ScoredChunk:
    """One retrieved chunk plus the collection it came from.

    Scatter-gather (Day 4b) merges hits from several per-file collections, so a
    hit must remember its origin collection — that's how the caller maps it back
    to a matter file_id (for the audit log and the "Drew on" label). `source` is
    the human filename from the payload and is what already drives citations;
    `collection` is the machine identity. Single-collection search does not need
    this and keeps returning raw Qdrant points."""

    collection: str
    score: float
    source: str
    locator: str
    text: str


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def default_collection_name(pdf_path: Path) -> str:
    return f"day1-{file_hash(pdf_path)[:16]}"


def connect(host: str, port: int) -> QdrantClient:
    return QdrantClient(host=host, port=port)


def collection_exists(client: QdrantClient, name: str) -> bool:
    return client.collection_exists(name)


def recreate_collection(client: QdrantClient, name: str, dim: int) -> None:
    if client.collection_exists(name):
        client.delete_collection(name)
    client.create_collection(
        collection_name=name,
        vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
    )


def upsert_chunks(
    client: QdrantClient,
    name: str,
    chunks: list[Chunk],
    vectors: list[list[float]],
) -> None:
    points = [
        PointStruct(
            id=i,
            vector=vec,
            payload={"source": c.source, "locator": c.locator, "text": c.text},
        )
        for i, (c, vec) in enumerate(zip(chunks, vectors))
    ]
    client.upsert(collection_name=name, points=points)


def search(client: QdrantClient, name: str, query_vec: list[float], top_k: int):
    result = client.query_points(
        collection_name=name, query=query_vec, limit=top_k, with_payload=True
    )
    return result.points


def search_across_collections(
    client: QdrantClient,
    collections: list[str],
    query_vec: list[float],
    top_k: int,
) -> list[ScoredChunk]:
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
    matter. An empty list yields an empty result."""
    merged: list[ScoredChunk] = []
    for name in collections:
        for p in client.query_points(
            collection_name=name, query=query_vec, limit=top_k, with_payload=True
        ).points:
            merged.append(
                ScoredChunk(
                    collection=name,
                    score=p.score,
                    source=p.payload["source"],
                    locator=p.payload["locator"],
                    text=p.payload["text"],
                )
            )
    merged.sort(key=lambda c: c.score, reverse=True)
    return merged[:top_k]


def retrieve_per_file_by_query(
    client: QdrantClient,
    collections: list[str],
    query_vec: list[float],
    top_k: int,
) -> dict[str, list[ScoredChunk]]:
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
    A failing collection propagates rather than being skipped — same reasoning
    as `search_across_collections`: a silent gap in a legal retrieval is worse
    than a loud failure."""
    out: dict[str, list[ScoredChunk]] = {}
    for name in collections:
        hits = client.query_points(
            collection_name=name, query=query_vec, limit=top_k, with_payload=True
        ).points
        out[name] = [
            ScoredChunk(
                collection=name,
                score=p.score,
                source=p.payload["source"],
                locator=p.payload["locator"],
                text=p.payload["text"],
            )
            for p in hits
        ]
    return out


def all_chunks(client: QdrantClient, name: str, batch: int = 256) -> list[str]:
    """Return the text of every chunk in a collection (no vectors).

    Used by the pleading limitation scan, which must see the whole document,
    not just the top-k retrieved for the drafting query."""
    texts: list[str] = []
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=name,
            with_payload=True,
            with_vectors=False,
            limit=batch,
            offset=offset,
        )
        texts.extend(p.payload.get("text", "") for p in points)
        if offset is None:
            break
    return texts
