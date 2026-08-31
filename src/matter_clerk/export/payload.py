"""The frozen snapshot an export is generated from (Day 4d).

Deliberately NOT the raw PipelineResult. Exports need a few things the result
does not carry (the matter name, the pleading party role, the friendly request
summary) and none of the things it carries for the retrieval layer's benefit
(collection, was_reindexed). A purpose-built payload gives the three generators
one stable contract, so adding a field for one format cannot perturb the
pipeline.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field

from ..citation import Citation
from ..structured import ComparisonTable, EntityCategory, TimelineRow

# Tasks whose output is inherently tabular. Code-owned, and the single source
# for BOTH the conditional Excel button and the endpoint's server-side refusal,
# so the UI and the server cannot disagree about what is exportable.
EXCEL_TASKS = {"timeline", "find_entities", "compare_clauses"}

# OOXML caps every core document property at 255 characters, and python-docx
# enforces it by raising. The full attribution names every source file, so it
# overflows on any matter with more than two or three real filenames — it
# belongs in the page footer, which has no such limit. See ARCHITECTURE
# 2026-08-14.
PROPERTY_LIMIT = 255


class ExportPayload(BaseModel):
    """Everything the docx/pdf/xlsx generators need, and nothing else.

    Treated as immutable once cached: two concurrent exports of one token read
    the same object, so no generator may mutate it.
    """

    task: str
    task_label: str

    # The answer as the lawyer read it on screen, with any structured JSON block
    # already stripped. Word/PDF render from this.
    answer_markdown: str
    citations: list[Citation] = Field(default_factory=list)

    # Structured intermediates — present only for the tabular tasks, and only
    # when the model returned valid data. Excel prefers these and falls back to
    # parsing answer_markdown when they are None.
    timeline_rows: list[TimelineRow] | None = None
    entity_categories: list[EntityCategory] | None = None
    comparison_table: ComparisonTable | None = None
    export_warnings: list[str] = Field(default_factory=list)

    # Attribution footer inputs
    matter_name: str | None = None
    source_files: list[str] = Field(default_factory=list)
    provenance_label: str = "Drew on"

    # Pleading safety machinery
    is_pleading: bool = False
    party_role: str | None = None
    pleading_warnings: list[str] = Field(default_factory=list)

    # Phase 2b: citation verification. `authority_mode` drives the exported
    # disclaimer and marker highlighting; `verification_summary` is the same
    # one-line status the lawyer saw on screen, so the file and the page cannot
    # disagree about what was checked.
    authority_mode: bool = False
    verification_summary: str = ""
    verification_incomplete: bool = False

    request_summary: dict[str, str] = Field(default_factory=dict)
    model: str = ""
    embed_model: str = ""
    top_k: int = 0
    timestamp: str = ""

    # ---------------------------------------------------------------- helpers
    @property
    def excel_available(self) -> bool:
        return self.task in EXCEL_TASKS

    @property
    def highlight_markers(self) -> bool:
        """Whether the body renderers should call out bracketed markers.

        Pleadings always do (the [ELEMENTS REQUIRED] gap markers). Authority
        mode adds a second, stronger reason that applies to memos too: a
        [REMOVED — citation not verified: ...] marker records that the tool
        DELETED a case the draft relied on. In an exported file that may be
        forwarded onward, that is the single most important thing on the page
        for a reviewer not to skim past."""
        return self.is_pleading or self.authority_mode

    def generated_on(self) -> str:
        """Date for the attribution footer, taken from the RUN timestamp rather
        than export time: the footer asserts when the analysis was produced, and
        a file exported days later must not claim to be that day's work."""
        try:
            return dt.datetime.fromisoformat(self.timestamp).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            return dt.date.today().isoformat()

    def attribution(self, *, max_files: int | None = None) -> str:
        """The full attribution line required in the BODY of every exported file.

        Belongs in the page footer, not a document property: it names every
        source file and so has no useful length bound. `max_files` elides the
        tail of the file list ("... and 4 more files") for the one caller that
        has a hard physical limit — the PDF footer, which is a fixed number of
        drawn lines. The date and the model name are never elided: they are the
        parts of the disclosure a reader needs to judge the output.
        """
        files = list(self.source_files)
        if not files:
            files_text = "(no files recorded)"
        elif max_files is not None and 0 < max_files < len(files):
            rest = len(files) - max_files
            files_text = (
                f"{', '.join(files[:max_files])} and {rest} more "
                f"file{'s' if rest != 1 else ''}"
            )
        else:
            files_text = ", ".join(files)
        return (
            f"Generated by Matter Clerk on {self.generated_on()}, "
            f"drawing on {files_text}. Model: {self.model}."
        )

    def short_attribution(self, limit: int = PROPERTY_LIMIT) -> str:
        """The attribution written to a document PROPERTY field.

        Identifies the tool, the matter and the date and stops there — under
        100 characters for any realistic matter name, and hard-clamped to
        `limit` so a pathological matter name cannot make an export raise. The
        full provenance (file list, model) lives in the document footer.
        """
        label = (self.matter_name or "").strip() or "export"
        date = self.generated_on()
        budget = limit - len(f"Matter Clerk -  - {date}")
        if len(label) > budget:
            label = label[: max(1, budget - 3)].rstrip() + "..."
        return f"Matter Clerk - {label} - {date}"


def build_payload(
    result,
    template,
    task: str,
    structured_inputs: dict,
    request_summary: dict,
    party_role: str | None,
    provenance_label: str,
    matter_name: str | None = None,
) -> ExportPayload:
    """Snapshot a PipelineResult for export.

    `source_files` falls back to the distinct sources on the citations because
    `retrieved_sources` is populated only in matter mode — without the fallback
    the attribution footer would be blank on exactly the ad-hoc single-file path
    where it is simplest to state.
    """
    report = getattr(result, "verification", None)
    sources = list(result.retrieved_sources)
    if not sources:
        seen: list[str] = []
        for c in result.citations:
            if c.source not in seen:
                seen.append(c.source)
        sources = seen

    return ExportPayload(
        task=task,
        task_label=template.label,
        answer_markdown=result.answer,
        citations=list(result.citations),
        timeline_rows=result.timeline_rows,
        entity_categories=result.entity_categories,
        comparison_table=result.comparison_table,
        export_warnings=list(result.export_warnings),
        matter_name=matter_name,
        source_files=sources,
        provenance_label=provenance_label,
        is_pleading=task == "draft_pleading",
        party_role=party_role,
        pleading_warnings=list(result.pleading_warnings),
        authority_mode=bool(getattr(result, "authority_mode", False)),
        verification_summary=report.summary_line() if report else "",
        verification_incomplete=bool(report.incomplete) if report else False,
        request_summary={str(k): str(v) for k, v in request_summary.items()},
        model=result.model,
        embed_model=result.embed_model,
        top_k=result.top_k,
        timestamp=result.timestamp,
    )
