from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path

from pydantic import BaseModel

from . import audit, pleadings
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
    all_chunks,
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


class LimitationReviewRequired(RuntimeError):
    """Raised before generating a pleading when the limitation check trips and
    the user has not confirmed a limitation analysis was completed."""

    def __init__(self, signals: list[str]) -> None:
        super().__init__("Limitation review required before drafting a pleading.")
        self.signals = signals


class PipelineResult(BaseModel):
    task: str
    answer: str
    citations: list[Citation]
    ocr_pages: list[int]
    unreadable_pages: list[int]
    pleading_warnings: list[str]
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

    pdf_sha256 = file_hash(pdf_path)

    # Pleading-specific safety gates (SoW 4.6.1): run BEFORE the model call so a
    # tripped limitation check refuses without spending tokens on a draft.
    pleading_warnings: list[str] = []
    if template.variants:
        pleading_type = structured_inputs.get("pleading_type")
        full_text = all_chunks(client, coll)

        # Scan the whole matter document AND the drafter's typed particulars:
        # the user's own description of the case is a prime place a limitation
        # concern (a deadline, a discovery date) gets mentioned.
        scan_text = full_text + [structured_inputs.get("claim_particulars") or ""]
        signals = pleadings.scan_for_limitation(scan_text)
        if signals:
            confirmed = bool(structured_inputs.get("limitation_confirmed"))
            audit.log_event(
                "limitation_review",
                task=task,
                pleading_type=pleading_type,
                source=source_name,
                pdf_sha256=pdf_sha256,
                signals=signals,
                user_confirmed=confirmed,
                proceeded=confirmed,
                claim_particulars=(structured_inputs.get("claim_particulars") or "")[
                    :2000
                ],
            )
            if not confirmed:
                raise LimitationReviewRequired(signals)

        if pleadings.role_for(pleading_type) == "Defendant" and not (
            pleadings.has_pleading_hallmarks(full_text)
        ):
            pleading_warnings.append(
                "The uploaded PDF does not contain typical pleading markers. "
                "You affirmed it is the opposing party's pleading — confirm that "
                "is correct before relying on this draft."
            )

    log.info("Asking the model ...")
    llm = LLMClient(model=model)
    answer = llm.complete(
        [
            {
                "role": "system",
                "content": build_system_prompt(template, structured_inputs),
            },
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
        pleading_warnings=pleading_warnings,
        model=model,
        embed_model=embed_model,
        top_k=resolved_top_k,
        timestamp=dt.datetime.now(dt.timezone.utc).isoformat(),
        pdf_sha256=pdf_sha256,
        collection=coll,
        was_reindexed=needs_index,
    )
