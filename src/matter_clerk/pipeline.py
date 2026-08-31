from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from typing import Optional

from pydantic import BaseModel

from . import audit, citations as case_citations, pleadings, structured, verification
from .citation import Citation
from .verification import VerificationReport
from .structured import ComparisonTable, EntityCategory, TimelineRow
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
    authority_mode_enabled,
    build_comparison_user_message,
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
    retrieve_per_file_by_query,
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

# --------------------------------------------------------------------------
# Compare Clauses retrieval budget (Day 4c).
#
# This task's retrieval is per-file, not merged-to-a-global-top-k, so context
# grows LINEARLY with the number of files compared — the opposite of the Day-4b
# scatter-gather, whose whole point is that a 20-file matter costs the same as
# one file. Two independent limits keep that in hand, and neither is silent:
#
#   COMPARE_MAX_FILES  A hard refusal above 20 files, surfaced in the form as a
#       warning telling the user to restrict the selection. Deliberately NOT a
#       silent truncation to the first 20: a comparison table that quietly omits
#       documents is a wrong answer that looks like a right one.
#   COMPARE_TOTAL_CHUNK_BUDGET  Reduces per-file DEPTH as the file count rises,
#       never the file COUNT. Every selected file keeps its column; it just
#       contributes fewer passages. Floored at COMPARE_MIN_PER_FILE_TOP_K,
#       below which a file cannot show a clause plus enough context to compare
#       it, so the floor wins and the budget is knowingly exceeded (logged).
#
# The effective per-file depth is reported on the result page rather than the
# template's nominal 6, so a reduced run is visible to the lawyer reading it.
# An explicit Advanced top_k is a per-file override and bypasses the budget.
#
# At the 20-file ceiling: floor(40/20)=2 -> floored to 3 -> 60 chunks, roughly
# 30k tokens at this codebase's ~500-token typical chunk. Comfortably inside the
# model window; the table itself is compact output.
# --------------------------------------------------------------------------
COMPARE_TASK_ID = "compare_clauses"
COMPARE_MAX_FILES = 20
COMPARE_TOTAL_CHUNK_BUDGET = 40
COMPARE_MIN_PER_FILE_TOP_K = 3


class QdrantUnreachable(RuntimeError):
    """Raised when Qdrant is not reachable at the configured host/port."""


class PdfHasNoText(RuntimeError):
    """Raised when extraction returns no usable text (no PDF page with text, or
    an email with an empty body)."""


class UnknownTask(RuntimeError):
    """Raised when an unrecognised task id is requested."""


class CompareClausesNotApplicable(RuntimeError):
    """Raised when Compare Clauses is asked to run over a file set it cannot
    honestly compare — fewer than 2 files, or more than COMPARE_MAX_FILES.

    The over-limit case is a refusal rather than a truncation on purpose: a
    comparison table silently missing documents reads as a complete answer."""


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
    # Day 4d: structured intermediates for the three tabular tasks, carried
    # alongside the markdown answer so exports do not have to parse legal
    # content back out of model prose. None for every other task, and None when
    # the model returned nothing valid (exports then fall back to the parser).
    timeline_rows: Optional[list[TimelineRow]] = None
    entity_categories: Optional[list[EntityCategory]] = None
    comparison_table: Optional[ComparisonTable] = None
    # Divergence between the structured data and the markdown table, surfaced to
    # the user rather than silently resolved in favour of either.
    export_warnings: list[str] = []
    # Phase 2b: citation verification. `authority_mode` is False for every task
    # and every run except a Draft Memo / Draft Pleading the lawyer explicitly
    # switched into authority mode, and `verification` is None whenever it is
    # False — so nothing downstream changes for any other run.
    authority_mode: bool = False
    verification: Optional[VerificationReport] = None

    model_config = {"arbitrary_types_allowed": True}


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
            chunks = chunk_pages(pages, source=source_name, ocr_pages=ocr_pages)
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
        matter_id=matter_id,
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
    # Day 4c: Compare Clauses supplies its own file-grouped user message (see
    # build_comparison_user_message). When None — every other task — the
    # standard flat-CONTEXT builder runs, byte-identically to before.
    user_message: str | None = None,
    # Single-file ingest provenance; matter mode omits these -> harmless defaults
    # (no fresh ingest happened, so there is no single sha/collection).
    ocr_pages: list[int] | None = None,
    unreadable_pages: list[int] | None = None,
    email_metadata: EmailMetadata | None = None,
    attachment_warnings: list[str] | None = None,
    pdf_sha256: str = "",
    collection: str = "",
    was_reindexed: bool = False,
    matter_id: int | None = None,
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
                "content": (
                    user_message
                    if user_message is not None
                    else build_user_message(template, structured_inputs, retrieved)
                ),
            },
        ]
    )

    # Day 4d: for the tabular tasks the completion carries a trailing ```json
    # block. Strip it before anything renders the answer (the web UI must look
    # exactly as it did) and validate it into the structured intermediates.
    # Non-tabular tasks pass through untouched.
    answer, extracted = structured.extract(task, answer.strip())

    # Phase 2b: citation verification. Runs AFTER generation and mutates only
    # `answer`, so it is invisible to every run that did not opt in — and so the
    # code-owned pleading machinery (DRAFT banner, cover note), which wraps the
    # answer at RENDER time, is unaffected by construction.
    authority = authority_mode_enabled(task, structured_inputs)
    report: VerificationReport | None = None
    if authority:
        answer, report = _verify_answer_citations(
            answer, task=task, matter_id=matter_id
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
        answer=answer,
        citations=citations,
        timeline_rows=extracted.timeline_rows,
        entity_categories=extracted.entity_categories,
        comparison_table=extracted.comparison_table,
        export_warnings=extracted.warnings,
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
        authority_mode=authority,
        verification=report,
    )


def _verify_answer_citations(
    answer: str, *, task: str, matter_id: int | None
) -> tuple[str, VerificationReport]:
    """Extract, verify, and rewrite the case citations in a generated answer.

    Never raises. A verification layer that can fail the whole run would mean a
    CanLII outage destroys a memo the model has already produced and the lawyer
    has already paid for — so every failure path lands in the report as
    UNVERIFIABLE and the draft is returned with honest markers instead."""
    try:
        found = case_citations.extract_citations(answer)
        report = verification.verify_citations(found)
        answer = verification.apply_to_answer(answer, report)
    except Exception as e:  # never lose a generated draft to a checking bug
        log.exception("Citation verification failed; returning the draft unmarked")
        report = VerificationReport(incomplete=True)
        report.results = []
        answer = (
            f"{answer}\n\n[UNVERIFIED — citation verification could not be "
            f"completed for this draft ({e}). No citation below has been "
            f"checked against CanLII.]"
        )
        return answer, report

    log.info(
        f"Citation verification: {report.summary_line()} "
        f"({report.calls_made} CanLII call(s))"
    )
    audit.log_event(
        "citation_verification",
        matter_id=matter_id,
        task=task,
        **verification.build_audit_payload(report),
    )
    return answer, report


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
        matter_id=matter_id,
    )


def compare_per_file_top_k(base_top_k: int, n_files: int) -> int:
    """Effective per-file retrieval depth for a Compare Clauses run.

    Shrinks depth so the total chunk count stays near COMPARE_TOTAL_CHUNK_BUDGET
    as the file count rises, but never below COMPARE_MIN_PER_FILE_TOP_K — a file
    contributing fewer than that cannot show a clause plus enough surrounding
    text to compare it, and a thin column is a worse outcome than exceeding the
    budget. Never reduces the number of FILES; every selected file keeps its
    column. Pure and separate so it can be checked without a live Qdrant."""
    if n_files <= 0:
        return base_top_k
    return max(COMPARE_MIN_PER_FILE_TOP_K, min(base_top_k, COMPARE_TOTAL_CHUNK_BUDGET // n_files))


def run_compare_clauses(
    files: list[MatterFile],
    structured_inputs: dict,
    matter_id: int,
    top_k: int | None = None,
) -> PipelineResult:
    """Per-file retrieve -> compare across a matter's documents (Day 4c).

    Structurally distinct from `run_matter_query` in all three of its stages,
    which is why it is a third entry point rather than a branch:

      * RETRIEVAL is per-file and kept grouped (`retrieve_per_file_by_query`),
        not merged into a global top-k. Every selected file contributes its own
        best passages about the clause, so no file can be outscored out of the
        comparison.
      * The PROMPT is built by `build_comparison_user_message`, which states
        which documents were searched — including ones that yielded nothing.
      * PROVENANCE means "checked", not "contributed": `retrieved_sources` and
        `retrieved_file_ids` list every file that was searched, even one the
        model ends up marking absent. A lawyer needs to know a document was
        looked at and found wanting; that is a finding, not a gap.

    `files` are the already-ingested matter files to compare, in the order their
    columns should appear — the caller (web handler) has already resolved and
    authorized any user subset selection. `top_k` is an explicit per-file
    override from the Advanced box and bypasses the chunk budget.

    No limitation gate: this task drafts nothing (the gate is pleading-specific
    and `compare_clauses` declares no `variants`, so the shared gate would not
    fire in any case)."""
    try:
        template = get_template(COMPARE_TASK_ID)
    except KeyError:
        raise UnknownTask(f"Unknown task: {COMPARE_TASK_ID!r}")

    if len(files) < 2:
        raise CompareClausesNotApplicable(
            "Compare Clauses needs at least 2 documents to compare; "
            f"{len(files)} selected."
        )
    if len(files) > COMPARE_MAX_FILES:
        raise CompareClausesNotApplicable(
            f"Compare Clauses is limited to {COMPARE_MAX_FILES} files; "
            f"{len(files)} selected. Use the file selector to restrict the "
            f"comparison to at most {COMPARE_MAX_FILES} files."
        )

    base_top_k = top_k if top_k is not None else template.top_k
    per_file_top_k = (
        top_k if top_k is not None else compare_per_file_top_k(base_top_k, len(files))
    )
    if per_file_top_k * len(files) > COMPARE_TOTAL_CHUNK_BUDGET:
        log.warning(
            f"Compare Clauses: {len(files)} files at the per-file floor of "
            f"{per_file_top_k} exceeds the {COMPARE_TOTAL_CHUNK_BUDGET}-chunk "
            f"budget ({per_file_top_k * len(files)} chunks)."
        )

    # Same filename twice in one matter is possible (the DB's uniqueness is on
    # content hash, not name) and would give the table two identically-headed
    # columns. Not renamed here: the column header must stay copy-exact with the
    # citation label, and diverging them would break citation verification.
    names = [f.filename for f in files]
    if len(set(names)) != len(names):
        log.warning(
            "Compare Clauses: two or more selected files share a filename; "
            "their table columns and citations will not be distinguishable."
        )

    host, port, embed_model, model = _config()
    client = connect(host, port)
    _precheck_qdrant(client)

    log.info(
        f"Comparing clauses across {len(files)} file(s), "
        f"top-{per_file_top_k} per file ..."
    )
    # Design decision: the user's "clauses to compare" text IS the retrieval
    # query, unreformulated. compare_clauses.yaml therefore carries no seed
    # `retrieval_query`, so this returns exactly what the user typed.
    retrieval_query = build_retrieval_query(template, structured_inputs)
    query_vec = embed([retrieval_query], model_name=embed_model)[0]
    by_collection = retrieve_per_file_by_query(
        client, [f.collection for f in files], query_vec, per_file_top_k
    )

    groups: list[tuple[str, list[dict]]] = []
    retrieved: list[dict] = []          # flat, in column order -> citations
    for f in files:
        chunks = [
            {"source": sc.source, "locator": sc.locator, "text": sc.text}
            for sc in by_collection.get(f.collection, [])
        ]
        groups.append((f.filename, chunks))
        retrieved.extend(chunks)

    if not retrieved:
        raise PdfHasNoText(
            "No passages about that clause were retrieved from any of the "
            "selected files."
        )

    return _answer_and_build(
        template=template,
        task=COMPARE_TASK_ID,
        structured_inputs=structured_inputs,
        retrieved=retrieved,
        model=model,
        embed_model=embed_model,
        resolved_top_k=per_file_top_k,
        pleading_warnings=[],
        cross_document=True,
        # Every file CHECKED, not merely every file cited.
        retrieved_sources=[f.filename for f in files],
        retrieved_file_ids=[f.id for f in files],
        user_message=build_comparison_user_message(
            template, structured_inputs, groups
        ),
        matter_id=matter_id,
    )
