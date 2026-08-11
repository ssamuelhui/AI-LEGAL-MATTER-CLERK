"""Structured intermediate representations for the tabular tasks (Day 4d).

Three of the eight tasks produce inherently tabular output. Until Day 4d the
only representation was the markdown table the model emitted, which meant an
Excel export had to parse legal content back out of model prose — a lossy step
in a system whose governing rule is that every output be verifiable.

So those three tasks now emit their rows as a fenced ```json block ALONGSIDE
the markdown table. Code extracts and validates it here, strips it from the
answer (so the web UI is visually unchanged), and stores it on PipelineResult.
Exports prefer the structured form; `export.tables` falls back to parsing the
markdown when a model returns malformed or absent JSON.

Because both representations are generated independently by the model, they can
disagree. `reconcile_*` compares them and returns human-readable warnings rather
than silently trusting either — a spreadsheet that quietly differs from the
answer the lawyer approved on screen is exactly the failure this project exists
to prevent.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re

from pydantic import BaseModel, Field, ValidationError

log = logging.getLogger("matter_clerk.structured")

# Task ids that carry a structured intermediate. Code-owned (not read from the
# YAML templates) for the same reason the safety preamble is: the set of tasks
# whose exports are structurally verifiable must not be editable by template work.
STRUCTURED_TASKS = {"timeline", "find_entities", "compare_clauses"}


# --------------------------------------------------------------------------
# Row models
# --------------------------------------------------------------------------
class TimelineRow(BaseModel):
    """One dated event.

    `date` is ALWAYS the date exactly as the document states it — timeline.yaml
    instructs the model to reproduce partial dates ("March 2024") and to note
    ambiguity verbatim. `date_iso` is set only where the date is complete and
    unambiguous, and is what the Excel export writes as a real date cell. Where
    it is None the spreadsheet keeps the verbatim text, because inventing a day
    to satisfy a column type would be fabrication in the column a lawyer is most
    likely to sort on.
    """

    date: str
    date_iso: str | None = None
    event: str
    source: str = ""
    significance: str = ""


class EntityRow(BaseModel):
    entity: str
    count: int | None = None
    source: str = ""


class EntityCategory(BaseModel):
    name: str
    rows: list[EntityRow] = Field(default_factory=list)
    note: str = ""  # e.g. "None found in the retrieved passages."


class ComparisonRow(BaseModel):
    """One attribute across every compared document. `cells` is positionally
    aligned to ComparisonTable.files; short rows are padded on validation so a
    column can never silently shift left."""

    attribute: str
    cells: list[str] = Field(default_factory=list)


class ComparisonTable(BaseModel):
    files: list[str] = Field(default_factory=list)
    rows: list[ComparisonRow] = Field(default_factory=list)

    def normalized(self) -> "ComparisonTable":
        """Pad/truncate every row to len(files). A ragged row from the model
        would otherwise shift cells into the wrong document's column — a
        silently wrong answer rather than a visibly broken one."""
        n = len(self.files)
        rows = []
        for r in self.rows:
            cells = list(r.cells[:n]) + [""] * max(0, n - len(r.cells))
            rows.append(ComparisonRow(attribute=r.attribute, cells=cells))
        return ComparisonTable(files=list(self.files), rows=rows)


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------
# The model is asked to append exactly one ```json fenced block. Match the LAST
# one: if a model echoes an example block before the real payload, the payload
# is the one that counts.
_JSON_BLOCK = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)


def split_structured_block(answer: str) -> tuple[str, dict | None]:
    """Return (answer_without_json_block, parsed_dict_or_None).

    Always strips the block from the answer even when it fails to parse — a raw
    JSON dump rendered into the result page would be noise to the lawyer, and
    the markdown table above it is still a complete, readable answer.
    """
    matches = list(_JSON_BLOCK.finditer(answer))
    if not matches:
        return answer, None
    m = matches[-1]
    stripped = (answer[: m.start()] + answer[m.end() :]).strip()
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        log.warning(f"Structured export block was not valid JSON ({e}); "
                    f"exports will fall back to parsing the markdown table.")
        return stripped, None
    return stripped, data if isinstance(data, dict) else None


def parse_timeline(data: dict | None) -> list[TimelineRow] | None:
    if not data or "events" not in data:
        return None
    try:
        rows = [TimelineRow(**r) for r in data["events"]]
    except (ValidationError, TypeError) as e:
        log.warning(f"Structured timeline rows failed validation ({e}).")
        return None
    for r in rows:
        r.date_iso = _coerce_iso(r.date_iso) or _coerce_iso(r.date)
    return rows


def parse_entities(data: dict | None) -> list[EntityCategory] | None:
    if not data or "categories" not in data:
        return None
    try:
        return [EntityCategory(**c) for c in data["categories"]]
    except (ValidationError, TypeError) as e:
        log.warning(f"Structured entity categories failed validation ({e}).")
        return None


def parse_comparison(data: dict | None) -> ComparisonTable | None:
    if not data or "files" not in data:
        return None
    try:
        return ComparisonTable(**data).normalized()
    except (ValidationError, TypeError) as e:
        log.warning(f"Structured comparison table failed validation ({e}).")
        return None


# Accepted ONLY as complete, unambiguous dates. Anything else stays verbatim
# text in the export. Deliberately narrow: a wrong guess here writes a date into
# a legal chronology that the source document never stated.
_ISO_FORMATS = ("%Y-%m-%d", "%d %B %Y", "%B %d, %Y", "%d %b %Y", "%b %d, %Y")


def _coerce_iso(value: str | None) -> str | None:
    if not value:
        return None
    v = " ".join(str(value).split())
    # A partial or hedged date ("March 2024", "on or about 3 May") must not be
    # coerced; the presence of qualifying words is itself a signal to leave it.
    if re.search(r"\b(or about|circa|approx|unclear|ambiguous|unknown)\b", v, re.I):
        return None
    for fmt in _ISO_FORMATS:
        try:
            return dt.datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------
# Reconciliation — structured vs. the markdown the lawyer actually reads
# --------------------------------------------------------------------------
class StructuredOutputs(BaseModel):
    """What `extract` recovered from one model completion."""

    timeline_rows: list[TimelineRow] | None = None
    entity_categories: list[EntityCategory] | None = None
    comparison_table: ComparisonTable | None = None
    warnings: list[str] = Field(default_factory=list)


_MD_ROW = re.compile(r"^\s*\|.*\|\s*$")
_MD_SEP = re.compile(r"^\s*\|?[\s:\-|]+\|[\s:\-|]*$")


def count_markdown_rows(md: str) -> int:
    """Total DATA rows across every pipe table in `md` (headers and separator
    rows excluded). Used only to reconcile against the structured count."""
    total = 0
    lines = md.splitlines()
    for i, line in enumerate(lines):
        if not _MD_ROW.match(line) or _MD_SEP.match(line):
            continue
        # A data row is one whose table has already had its separator.
        if any(_MD_SEP.match(prev) for prev in lines[max(0, i - 40) : i]):
            total += 1
    return total


def extract(task: str, answer: str) -> tuple[str, StructuredOutputs]:
    """Pull the structured block out of a completion for a tabular task.

    Returns (answer_without_the_block, outputs). For non-tabular tasks the
    answer is returned unchanged and outputs are empty, so the seven other tasks
    are byte-identical to pre-Day-4d.
    """
    if task not in STRUCTURED_TASKS:
        return answer, StructuredOutputs()

    stripped, data = split_structured_block(answer)
    out = StructuredOutputs()
    if data is None:
        out.warnings.append(
            "This result carried no valid structured export data, so a "
            "spreadsheet export falls back to reading the table out of the "
            "answer text. Check the exported file against the answer."
        )
        return stripped, out

    md_rows = count_markdown_rows(stripped)
    if task == "timeline":
        out.timeline_rows = parse_timeline(data)
        if out.timeline_rows is not None:
            out.warnings += reconcile_row_count(
                "timeline", len(out.timeline_rows), md_rows
            )
    elif task == "find_entities":
        out.entity_categories = parse_entities(data)
        if out.entity_categories is not None:
            n = sum(len(c.rows) for c in out.entity_categories)
            out.warnings += reconcile_row_count("entity", n, md_rows)
    elif task == "compare_clauses":
        out.comparison_table = parse_comparison(data)
        if out.comparison_table is not None:
            out.warnings += reconcile_row_count(
                "comparison", len(out.comparison_table.rows), md_rows
            )

    if (
        out.timeline_rows is None
        and out.entity_categories is None
        and out.comparison_table is None
    ):
        out.warnings.append(
            "The structured export data for this result did not validate, so a "
            "spreadsheet export falls back to reading the table out of the "
            "answer text. Check the exported file against the answer."
        )
    return stripped, out


def reconcile_row_count(kind: str, structured_n: int, markdown_n: int) -> list[str]:
    """Compare the structured row count against the markdown table's.

    A mismatch means the exported file and the on-screen answer disagree about
    how many facts there are. That is surfaced to the user, never resolved
    silently in favour of one side."""
    if markdown_n and structured_n != markdown_n:
        return [
            f"Export note: the {kind} table on screen has {markdown_n} row(s) "
            f"but the structured export data has {structured_n}. The exported "
            f"file follows the structured data. Verify against the source "
            f"before relying on the export."
        ]
    return []
