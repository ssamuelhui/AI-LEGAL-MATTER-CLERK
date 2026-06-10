from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path

from pydantic import BaseModel

from .citation import Citation
from .embed import embed, embedding_dimension
from .ingest import chunk_pages, extract_pdf_pages
from .llm import LLMClient
from .prompts import (
    build_retrieval_query,
    build_system_prompt,
    build_user_message,
    get_template,
)
from .vectorstore import (
    collection_exists,
    connect,
    default_collection_name,
    file_hash,
    recreate_collection,
    search,
    upsert_chunks,
)

log = logging.getLogger("matter_clerk")


class QdrantUnreachable(RuntimeError):
    """Raised when Qdrant is not reachable at the configured host/port."""


class PdfHasNoText(RuntimeError):
    """Raised when extraction returns zero pages with text."""


class UnknownTask(RuntimeError):
    """Raised when an unrecognised task id is requested."""


class PipelineResult(BaseModel):
    task: str
    answer: str
    citations: list[Citation]
    ocr_pages: list[int]
    unreadable_pages: list[int]
    model: str
    embed_model: str
    top_k: int
    timestamp: str
    pdf_sha256: str
    collection: str
    was_reindexed: bool


def _precheck_qdrant(client) -> None:
    try:
        client.get_collections()
    except Exception as e:
        raise QdrantUnreachable(str(e))


def run_query(
    pdf_path: Path,
    source_name: str,
    task: str,
    structured_inputs: dict,
    top_k: int | None = None,
    reindex: bool = False,
    collection: str | None = None,
) -> PipelineResult:
    """Ingest -> retrieve -> answer for one PDF and one task.

    Shared by the CLI and the Flask handler. `source_name` is the human-facing
    filename that appears in citations (e.g. "Pleadings_2024-03-15.pdf"); it
    can differ from `pdf_path` because the web handler hands us a temp file but
    we want the upload's original name in the citation.

    `task` selects a TaskTemplate (e.g. "summarize"); `structured_inputs` holds
    that task's user inputs (e.g. {"question": "..."}). `top_k` overrides the
    template's per-task default when supplied.
    """
    try:
        template = get_template(task)
    except KeyError:
        raise UnknownTask(f"Unknown task: {task!r}")

    resolved_top_k = top_k if top_k is not None else template.top_k

    host = os.environ.get("QDRANT_HOST", "localhost")
    port = int(os.environ.get("QDRANT_PORT", "6333"))
    embed_model = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
    model = os.environ.get("MODEL", "xiaomi/mimo-v2.5-pro")

    client = connect(host, port)
    _precheck_qdrant(client)

    coll = collection or default_collection_name(pdf_path)
    needs_index = reindex or not collection_exists(client, coll)
    ocr_pages: list[int] = []
    unreadable_pages: list[int] = []

    if needs_index:
        log.info(f"Ingesting {source_name} ...")
        pages, ocr_pages, unreadable_pages = extract_pdf_pages(pdf_path)
        log.info(
            f"Extracted {len(pages)} page(s) "
            f"({len(ocr_pages)} via OCR, {len(unreadable_pages)} unreadable)"
        )
        if not pages:
            raise PdfHasNoText(
                f"No extractable text on any page of {source_name}."
            )
        chunks = chunk_pages(pages, source=source_name)
        log.info(f"Embedding {len(chunks)} chunks with {embed_model} ...")
        vectors = embed([c.text for c in chunks], model_name=embed_model)
        recreate_collection(client, coll, dim=embedding_dimension(embed_model))
        upsert_chunks(client, coll, chunks, vectors)
        log.info(f"Indexed into collection: {coll}")
    else:
        log.info(f"Reusing existing collection: {coll}")

    log.info("Retrieving relevant chunks ...")
    retrieval_query = build_retrieval_query(template, structured_inputs)
    query_vec = embed([retrieval_query], model_name=embed_model)[0]
    hits = search(client, coll, query_vec, resolved_top_k)
    retrieved = [
        {
            "source": h.payload["source"],
            "page": h.payload["page"],
            "text": h.payload["text"],
        }
        for h in hits
    ]
    if not retrieved:
        raise PdfHasNoText("No chunks retrieved.")

    log.info("Asking the model ...")
    llm = LLMClient(model=model)
    answer = llm.complete(
        [
            {"role": "system", "content": build_system_prompt(template)},
            {
                "role": "user",
                "content": build_user_message(template, structured_inputs, retrieved),
            },
        ]
    )

    citations: list[Citation] = []
    seen: set[tuple[str, str]] = set()
    for c in retrieved:
        page_label = f"p.{c['page']}"
        key = (c["source"], page_label)
        if key in seen:
            continue
        seen.add(key)
        snippet = c["text"].strip().replace("\n", " ")
        if len(snippet) > 160:
            snippet = snippet[:160] + "..."
        citations.append(
            Citation(
                source=c["source"],
                page_or_paragraph=page_label,
                text_snippet=snippet,
            )
        )

    return PipelineResult(
        task=task,
        answer=answer.strip(),
        citations=citations,
        ocr_pages=ocr_pages,
        unreadable_pages=unreadable_pages,
        model=model,
        embed_model=embed_model,
        top_k=resolved_top_k,
        timestamp=dt.datetime.now(dt.timezone.utc).isoformat(),
        pdf_sha256=file_hash(pdf_path),
        collection=coll,
        was_reindexed=needs_index,
    )
