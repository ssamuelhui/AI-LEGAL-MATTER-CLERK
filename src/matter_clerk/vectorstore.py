from __future__ import annotations

import hashlib
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from .ingest import Chunk


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
            payload={"source": c.source, "page": c.page, "text": c.text},
        )
        for i, (c, vec) in enumerate(zip(chunks, vectors))
    ]
    client.upsert(collection_name=name, points=points)


def search(client: QdrantClient, name: str, query_vec: list[float], top_k: int):
    result = client.query_points(
        collection_name=name, query=query_vec, limit=top_k, with_payload=True
    )
    return result.points


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
