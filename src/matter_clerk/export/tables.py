"""One table shape for all three export formats (Day 4d).

Word, PDF and Excel all need "a titled grid of strings". Deriving that once,
here, is what keeps the three generators from each growing their own opinion
about what a Timeline row is.

Source preference is structured-first with a markdown fallback:

  1. The validated structured intermediate from the model (`payload.*`), when
     present. Robust, and carries information markdown cannot — notably which
     dates are complete enough to write as real Excel date cells.
  2. Otherwise the markdown table parsed back out of the answer. Lossy, so it
     is a degradation path, not the design.

The parser is deliberately STRICT about arity. A row whose cell count does not
match the header is kept verbatim and counted, never reshaped to fit: silently
padding or truncating a row would move legal content into the wrong column,
which reads as a complete answer while being wrong.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from .payload import ExportPayload

TIMELINE_HEADERS = ["Date", "Event", "Source", "Procedural significance"]
ENTITY_HEADERS = ["Entity", "Count", "Source"]


class ExportTable(BaseModel):
    """A titled grid, ready to write into any of the three formats."""

    title: str | None = None
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    note: str = ""
    # Excel-only enrichment: which column holds dates, and the ISO value for
    # those rows whose date was complete and unambiguous. Rows absent from
    # `date_values` keep their verbatim text.
    date_column: int | None = None
    date_values: dict[int, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Markdown fallback parser
# --------------------------------------------------------------------------
_SEP = re.compile(r"^\s*\|?[\s:\-|]+\|[\s:\-|]*$")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def parse_markdown_tables(md: str) -> list[ExportTable]:
    """Extract every pipe table, tagged with its nearest preceding heading.

    Ragged rows are preserved verbatim (padded only for rendering) and recorded
    in `note` so the discrepancy reaches the reader instead of being hidden.
    """
    out: list[ExportTable] = []
    heading: str | None = None
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if m := _HEADING.match(line):
            heading = m.group(2).strip()
            i += 1
            continue
        is_row = line.strip().startswith("|") and "|" in line.strip()[1:]
        if is_row and i + 1 < len(lines) and _SEP.match(lines[i + 1]):
            headers = _split_row(line)
            rows: list[list[str]] = []
            ragged = 0
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                cells = _split_row(lines[j])
                if len(cells) != len(headers):
                    ragged += 1
                    cells = (cells + [""] * len(headers))[: len(headers)]
                rows.append(cells)
                j += 1
            note = (
                f"{ragged} row(s) in the source table did not match the column "
                f"count and are shown as parsed — verify against the answer."
                if ragged
                else ""
            )
            out.append(
                ExportTable(title=heading, headers=headers, rows=rows, note=note)
            )
            i = j
            continue
        i += 1
    return out


# --------------------------------------------------------------------------
# Per-task table construction
# --------------------------------------------------------------------------
def timeline_tables(payload: ExportPayload) -> list[ExportTable]:
    if payload.timeline_rows:
        rows: list[list[str]] = []
        date_values: dict[int, str] = {}
        for idx, r in enumerate(payload.timeline_rows):
            rows.append([r.date, r.event, r.source, r.significance])
            if r.date_iso:
                date_values[idx] = r.date_iso
        return [
            ExportTable(
                title="Timeline",
                headers=list(TIMELINE_HEADERS),
                rows=rows,
                date_column=0,
                date_values=date_values,
            )
        ]
    tables = parse_markdown_tables(payload.answer_markdown)
    for t in tables:
        t.title = t.title or "Timeline"
        t.date_column = 0
    return tables


def entity_tables(payload: ExportPayload) -> list[ExportTable]:
    if payload.entity_categories:
        return [
            ExportTable(
                title=cat.name,
                headers=list(ENTITY_HEADERS),
                rows=[
                    [e.entity, "" if e.count is None else str(e.count), e.source]
                    for e in cat.rows
                ],
                note=cat.note,
            )
            for cat in payload.entity_categories
        ]
    return parse_markdown_tables(payload.answer_markdown)


def comparison_tables(payload: ExportPayload) -> list[ExportTable]:
    if payload.comparison_table and payload.comparison_table.files:
        ct = payload.comparison_table.normalized()
        return [
            ExportTable(
                title="Clause comparison",
                headers=["Attribute", *ct.files],
                rows=[[r.attribute, *r.cells] for r in ct.rows],
            )
        ]
    tables = parse_markdown_tables(payload.answer_markdown)
    for t in tables:
        t.title = t.title or "Clause comparison"
    return tables


_BUILDERS = {
    "timeline": timeline_tables,
    "find_entities": entity_tables,
    "compare_clauses": comparison_tables,
}


def tables_for(payload: ExportPayload) -> list[ExportTable]:
    """Every table this payload's task should export. Empty for prose tasks."""
    builder = _BUILDERS.get(payload.task)
    return builder(payload) if builder else []


def used_structured(payload: ExportPayload) -> bool:
    """Whether the structured intermediate (rather than the markdown fallback)
    is what the export is built from. Reported on the Excel metadata sheet so a
    degraded export is visible rather than merely different."""
    return bool(
        (payload.task == "timeline" and payload.timeline_rows)
        or (payload.task == "find_entities" and payload.entity_categories)
        or (
            payload.task == "compare_clauses"
            and payload.comparison_table
            and payload.comparison_table.files
        )
    )
