from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from typing import Optional

from pydantic import BaseModel

from . import audit, pleadings
from .citation import Citation
from .embed import embed, embedding_dimension
from .ingest import (
    EmailMetadata,
    chunk_email,
    chunk_pages,
    extract_email,
    extract_pdf_pages,
)
from .llm import LLMClient
from .matters import MatterFile
from .prompts import (
    TaskTemplate,
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
    search_across_collections,
    upsert_chunks,
)

log = logging.getLogger("matter_clerk")

# Day 4c-a: matter-mode Timeline "Detailed" retrieval depth. The lawyers' issue
# is that a global top_k spread across N files loses per-file detail versus
# single-file mode. Detailed raises the global top_k (which, per the Day-4b
# scatter-gather, is ALSO the per-file fetch depth — one knob preserves the
# losslessness guarantee: per_file >= global). 28 ~= 2x the Timeline template
# default of 14; at ~700 tokens/chunk that is ~13-15k tokens typical, well
# inside the model's context window. Only reached in matter mode; single-file is
# already exhaustive within top_k, so Detailed changes only the prompt there. An
# explicit Advanced top_k override still wins over this default.
DETAILED_MATTER_TOP_K = 28


class QdrantUnreachable(RuntimeError):
    """Raised when Qdrant is not reachable at the configured host/port."""


class PdfHasNoText(RuntimeError):
    """Raised when extraction returns no usable text (no PDF page with text, or
    an email with an empty body)."""


class UnknownTask(RuntimeError):
    """Raised when an unrecognised task id is requested."""


@dataclass
class FileLimitationSignals:
    """Limitation signals attributed to one source — a matter file, or the
    user's typed claim particulars. Drives the matter-mode refusal banner (which
    names WHICH files tripped) and the audit log's limitation_files list."""

    file_id: int | None   # None == the typed claim particulars (not a file)
    label: str            # filename, or "(your claim particulars)"
    signals: list[str]


class LimitationReviewRequired(RuntimeError):
    """Raised before generating a pleading when the limitation check trips and
    the user has not confirmed a limitation analysis was completed."""

    def __init__(
        self,
        signals: list[str],
        signals_by_file: list[FileLimitationSignals] | None = None,
    ) -> None:
        super().__init__("Limitation review required before drafting a pleading.")
        # Flat union of every signal — the contract relied on by the single-file
        # and ad-hoc refusal banner, unchanged. `signals_by_file` is populated
        # only in matter mode so the banner can name which file each came from.
        self.signals = signals
        self.signals_by_file = signals_by_file


class PipelineResult(BaseModel):
    task: str
    answer: str
    citations: list[Citation]
    ocr_pages: list[int]
    unreadable_pages: list[int]
    pleading_warnings: list[str]
    email_metadata: Optional[EmailMetadata] = None
    attachment_warnings: list[str] = []
    model: str
    embed_model: str
    top_k: int
    timestamp: str
    pdf_sha256: str
    collection: str
    was_reindexed: bool
    # Day 4b: matter scatter-gather. cross_document drives the "Drew on" label
    # and (Step 4) the matter-aware prompt; the two lists record which files
    # actually grounded the answer. All default to single-file/ad-hoc values so
    # existing callers are unaffected.
    cross_document: bool = False
    retrieved_sources: list[str] = []
    retrieved_file_ids: list[int] = []


class IngestOutcome(BaseModel):
    """Result of ingesting (or reusing a cached collection for) one file.

    Returned by `ingest_file` and consumed both by `run_query` (which then
    retrieves + answers) and by the matter upload path (which only ingests and
    records the result in the manifest)."""

    collection: str
    sha256: str
    was_reindexed: bool
    chunk_count: int
    ocr_pages: list[int] = []
    unreadable_pages: list[int] = []
    email_metadata: Optional[EmailMetadata] = None
    attachment_warnings: list[str] = []


def _config() -> tuple[str, int, str, str]:
    """(qdrant_host, qdrant_port, embed_model, llm_model) from the environment."""
    host = os.environ.get("QDRANT_HOST", "localhost")
    port = int(os.environ.get("QDRANT_PORT", "6333"))
    embed_model = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
    model = os.environ.get("MODEL", "xiaomi/mimo-v2.5-pro")
    return host, port, embed_model, model


def _precheck_qdrant(client) -> None:
    try:
        client.get_collections()
    except Exception as e:
        raise QdrantUnreachable(str(e))


def ingest_file(
    path: Path,
    source_name: str,
    collection: str | None = None,
    reindex: bool = False,
) -> IngestOutcome:
    """Ingest one PDF or .eml into a Qdrant collection (or reuse a cached one).

    `collection` pins the target name; when None it derives the ad-hoc
    content-hash name (`default_collection_name`). The matter path passes the
    persisted `m<matter_id>-<sha16>` name so the same content in two matters
    lands in two collections. Email metadata and attachment names are parsed on
    every call (even a cache hit) because the result page surfaces them. Raises
    PdfHasNoText when there is nothing to index.
    """
    host, port, embed_model, _model = _config()
    client = connect(host, port)
    _precheck_qdrant(client)

    is_email = path.suffix.lower() == ".eml"
    coll = collection or default_collection_name(path)
    needs_index = reindex or not collection_exists(client, coll)

    ocr_pages: list[int] = []
    unreadable_pages: list[int] = []
    email_metadata: EmailMetadata | None = None
    attachment_warnings: list[str] = []
    email_body = ""

    if is_email:
        email_metadata, email_body, attachment_warnings = extract_email(path)

    chunk_count = 0
    if needs_index:
        log.info(f"Ingesting {source_name} ...")
        if is_email:
            if not email_body.strip():
                raise PdfHasNoText(f"No extractable text in the body of {source_name}.")
            chunks = chunk_email(
                email_body, source=source_name, locator=email_metadata.locator
            )
            log.info(f"Extracted email body ({len(email_body)} chars)")
        else:
            pages, ocr_pages, unreadable_pages = extract_pdf_pages(path)
            log.info(
                f"Extracted {len(pages)} page(s) "
                f"({len(ocr_pages)} via OCR, {len(unreadable_pages)} unreadable)"
            )
            if not pages:
                raise PdfHasNoText(f"No extractable text on any page of {source_name}.")
            chunks = chunk_pages(pages, source=source_name)
        log.info(f"Embedding {len(chunks)} chunks with {embed_model} ...")
        vectors = embed([c.text for c in chunks], model_name=embed_model)
        recreate_collection(client, coll, dim=embedding_dimension(embed_model))
        upsert_chunks(client, coll, chunks, vectors)
        chunk_count = len(chunks)
        log.info(f"Indexed into collection: {coll}")
    else:
        log.info(f"Reusing existing collection: {coll}")

    return IngestOutcome(
        collection=coll,
        sha256=file_hash(path),
        was_reindexed=needs_index,
        chunk_count=chunk_count,
        ocr_pages=ocr_pages,
        unreadable_pages=unreadable_pages,
        email_metadata=email_metadata,
        attachment_warnings=attachment_warnings,
    )


def run_query(
    pdf_path: Path,
    source_name: str,
    task: str,
    structured_inputs: dict,
    top_k: int | None = None,
    reindex: bool = False,
    collection: str | None = None,
    matter_id: int | None = None,
) -> PipelineResult:
    """Ingest -> retrieve -> answer for one PDF and one task.

    Shared by the CLI and the Flask handler. `source_name` is the human-facing
    filename that appears in citations (e.g. "Pleadings_2024-03-15.pdf"); it
    can differ from `pdf_path` because the web handler hands us a temp file but
    we want the upload's original name in the citation.

    `task` selects a TaskTemplate (e.g. "summarize"); `structured_inputs` holds
    that task's user inputs (e.g. {"question": "..."}). `top_k` overrides the
    template's per-task default when supplied.

    `matter_id` is the id of the matter this query runs inside, or None for the
    ad-hoc single-file path. It only flows into the audit log (Day 4a); the
    retrieve->prompt->cite core is unchanged and still single-collection. When
    querying a file already in a matter the caller passes the file's persisted
    `collection` so no re-ingest occurs.
    """
    try:
        template = get_template(task)
    except KeyError:
        raise UnknownTask(f"Unknown task: {task!r}")

    resolved_top_k = top_k if top_k is not None else template.top_k

    host, port, embed_model, model = _config()

    # Ingest (or reuse the cached collection). For a matter file the caller
    # passes its persisted `collection`, so this is a no-op cache hit and only
    # the email metadata gets re-parsed for the result page.
    outcome = ingest_file(
        pdf_path, source_name, collection=collection, reindex=reindex
    )
    coll = outcome.collection

    client = connect(host, port)
    _precheck_qdrant(client)

    log.info("Retrieving relevant chunks ...")
    retrieval_query = build_retrieval_query(template, structured_inputs)
    query_vec = embed([retrieval_query], model_name=embed_model)[0]
    hits = search(client, coll, query_vec, resolved_top_k)
    retrieved = [
        {
            "source": h.payload["source"],
            "locator": h.payload["locator"],
            "text": h.payload["text"],
        }
        for h in hits
    ]
    if not retrieved:
        raise PdfHasNoText("No chunks retrieved.")

    pdf_sha256 = outcome.sha256

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
                matter_id=matter_id,
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

    return _answer_and_build(
        template=template,
        task=task,
        structured_inputs=structured_inputs,
        retrieved=retrieved,
        model=model,
        embed_model=embed_model,
        resolved_top_k=resolved_top_k,
        pleading_warnings=pleading_warnings,
        ocr_pages=outcome.ocr_pages,
        unreadable_pages=outcome.unreadable_pages,
        email_metadata=outcome.email_metadata,
        attachment_warnings=outcome.attachment_warnings,
        pdf_sha256=pdf_sha256,
        collection=coll,
        was_reindexed=outcome.was_reindexed,
    )


def _answer_and_build(
    *,
    template: TaskTemplate,
    task: str,
    structured_inputs: dict,
    retrieved: list[dict],
    model: str,
    embed_model: str,
    resolved_top_k: int,
    pleading_warnings: list[str],
    cross_document: bool = False,
    retrieved_sources: list[str] | None = None,
    retrieved_file_ids: list[int] | None = None,
    # Single-file ingest provenance; matter mode omits these -> harmless defaults
    # (no fresh ingest happened, so there is no single sha/collection).
    ocr_pages: list[int] | None = None,
    unreadable_pages: list[int] | None = None,
    email_metadata: EmailMetadata | None = None,
    attachment_warnings: list[str] | None = None,
    pdf_sha256: str = "",
    collection: str = "",
    was_reindexed: bool = False,
) -> PipelineResult:
    """Shared answer/citation/result tail for run_query and run_matter_query.

    Builds the system + user prompt, calls the LLM, dedups citations, and
    assembles the PipelineResult. `cross_document` is stored on the result so the
    UI can render the "Drew on" label; Step 4 will also thread it into
    build_system_prompt. None-valued lists coerce to [] (no shared-mutable
    default)."""
    log.info("Asking the model ...")
    llm = LLMClient(model=model)
    answer = llm.complete(
        [
            {
                "role": "system",
                "content": build_system_prompt(
                    template, structured_inputs, cross_document=cross_document
                ),
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
        locator = c["locator"]
        key = (c["source"], locator)
        if key in seen:
            continue
        seen.add(key)
        snippet = c["text"].strip().replace("\n", " ")
        if len(snippet) > 160:
            snippet = snippet[:160] + "..."
        citations.append(
            Citation(
                source=c["source"],
                page_or_paragraph=locator,
                text_snippet=snippet,
            )
        )

    return PipelineResult(
        task=task,
        answer=answer.strip(),
        citations=citations,
        ocr_pages=ocr_pages or [],
        unreadable_pages=unreadable_pages or [],
        pleading_warnings=pleading_warnings,
        email_metadata=email_metadata,
        attachment_warnings=attachment_warnings or [],
        model=model,
        embed_model=embed_model,
        top_k=resolved_top_k,
        timestamp=dt.datetime.now(dt.timezone.utc).isoformat(),
        pdf_sha256=pdf_sha256,
        collection=collection,
        was_reindexed=was_reindexed,
        cross_document=cross_document,
        retrieved_sources=retrieved_sources or [],
        retrieved_file_ids=retrieved_file_ids or [],
    )


def _scan_matter_for_limitation(
    matter_texts: list[tuple[MatterFile, list[str]]],
    claim_particulars: str,
) -> list[FileLimitationSignals]:
    """Run the (pure, unchanged) limitation scanner against EVERY file in the
    matter plus the user's typed claim particulars, attributing signals to their
    source. Files with no signals are omitted. Takes pre-fetched chunk texts so
    each collection is scrolled exactly once per request (the caller reuses the
    same texts for the defendant hallmarks check). Used only by the matter-mode
    pleading gate; the single-file/ad-hoc gate in run_query is untouched."""
    out: list[FileLimitationSignals] = []
    for f, texts in matter_texts:
        sigs = pleadings.scan_for_limitation(texts)
        if sigs:
            out.append(FileLimitationSignals(f.id, f.filename, sigs))
    if claim_particulars:
        sigs = pleadings.scan_for_limitation([claim_particulars])
        if sigs:
            out.append(
                FileLimitationSignals(None, "(your claim particulars)", sigs)
            )
    return out


def run_matter_query(
    files: list[MatterFile],
    task: str,
    structured_inputs: dict,
    matter_id: int,
    top_k: int | None = None,
) -> PipelineResult:
    """Scatter-gather retrieve -> answer across every file in a matter (Day 4b).

    No ingest (files are already in Qdrant). Retrieves top_k from each file's
    collection, merges by score to a global top_k, runs the limitation gate
    across ALL files in matter mode, and builds a PipelineResult with
    cross_document=True. Shares the answer/citation tail with run_query.

    `files` are the matter's already-ingested files (each carries its persisted
    `collection`). The single-file-in-matter path stays on run_query with a
    pinned collection; this function is reached only when the user queries the
    whole matter (no specific file selected)."""
    try:
        template = get_template(task)
    except KeyError:
        raise UnknownTask(f"Unknown task: {task!r}")
    if not files:
        raise PdfHasNoText("This matter has no ingested files to query.")

    # Day 4c-a: the Timeline "Detailed" control raises matter-mode retrieval depth.
    # An explicit Advanced top_k override always wins; otherwise Detailed uses
    # DETAILED_MATTER_TOP_K and everything else falls back to the template default.
    detail_level = structured_inputs.get("detail_level") or "Concise"
    if top_k is not None:
        resolved_top_k = top_k
    elif detail_level == "Detailed":
        resolved_top_k = DETAILED_MATTER_TOP_K
    else:
        resolved_top_k = template.top_k
    host, port, embed_model, model = _config()

    client = connect(host, port)
    _precheck_qdrant(client)

    collections = [f.collection for f in files]
    coll_to_file_id = {f.collection: f.id for f in files}

    log.info(f"Retrieving across {len(collections)} matter file(s) ...")
    retrieval_query = build_retrieval_query(template, structured_inputs)
    query_vec = embed([retrieval_query], model_name=embed_model)[0]
    scored = search_across_collections(client, collections, query_vec, resolved_top_k)
    if not scored:
        raise PdfHasNoText("No chunks retrieved from any file in this matter.")

    retrieved = [
        {"source": sc.source, "locator": sc.locator, "text": sc.text} for sc in scored
    ]
    # Files that actually contributed to the final top-k, in score order — drives
    # the "Drew on" label and the audit retrieved_file_ids.
    retrieved_sources: list[str] = []
    retrieved_file_ids: list[int] = []
    for sc in scored:
        if sc.source not in retrieved_sources:
            retrieved_sources.append(sc.source)
        fid = coll_to_file_id.get(sc.collection)
        if fid is not None and fid not in retrieved_file_ids:
            retrieved_file_ids.append(fid)

    # Pleading safety gates (SoW 4.6.1), matter-wide: the limitation scan reads
    # EVERY file in the matter (not just the retrieved top-k), so more text is
    # scanned and more signals fire — consistent with the false-positive-friendly
    # design. Runs BEFORE the model call so a tripped gate refuses for free.
    pleading_warnings: list[str] = []
    if template.variants:
        pleading_type = structured_inputs.get("pleading_type")
        claim_particulars = structured_inputs.get("claim_particulars") or ""
        # Scroll every collection exactly once; reuse the texts for both the
        # limitation scan and the defendant hallmarks check.
        matter_texts = [(f, all_chunks(client, f.collection)) for f in files]
        per_file = _scan_matter_for_limitation(matter_texts, claim_particulars)
        if per_file:
            flat: list[str] = []  # order-preserving union across files
            for fs in per_file:
                for s in fs.signals:
                    if s not in flat:
                        flat.append(s)
            limitation_files = [
                fs.file_id for fs in per_file if fs.file_id is not None
            ]
            confirmed = bool(structured_inputs.get("limitation_confirmed"))
            audit.log_event(
                "limitation_review",
                task=task,
                matter_id=matter_id,
                pleading_type=pleading_type,
                source=None,
                retrieved_file_ids=retrieved_file_ids,
                limitation_files=limitation_files,
                signals=flat,
                user_confirmed=confirmed,
                proceeded=confirmed,
                claim_particulars=claim_particulars[:2000],
            )
            if not confirmed:
                raise LimitationReviewRequired(flat, signals_by_file=per_file)

        if pleadings.role_for(pleading_type) == "Defendant":
            all_texts = [t for _f, texts in matter_texts for t in texts]
            if not pleadings.has_pleading_hallmarks(all_texts):
                pleading_warnings.append(
                    "The matter's files do not contain typical pleading markers. "
                    "You affirmed the opposing party's pleading is in this matter "
                    "— confirm that is correct before relying on this draft."
                )

    return _answer_and_build(
        template=template,
        task=task,
        structured_inputs=structured_inputs,
        retrieved=retrieved,
        model=model,
        embed_model=embed_model,
        resolved_top_k=resolved_top_k,
        pleading_warnings=pleading_warnings,
        cross_document=True,
        retrieved_sources=retrieved_sources,
        retrieved_file_ids=retrieved_file_ids,
    )
