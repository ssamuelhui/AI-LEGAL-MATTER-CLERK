"""PDF generation: a Story of Flowables, dispatched per task (Day 4d).

Structurally parallel to `docx_render` — same dispatcher, same eight renderers,
same safety contract:

THE DRAFT banner and cover note are added by `build_pdf`, keyed off
`payload.is_pleading`, NOT inside `render_pleading`. On top of that the diagonal
watermark and the per-page DRAFT footer live in the PAGE TEMPLATE rather than
the story, so they are not content and cannot be removed by editing content.
"""

from __future__ import annotations

import io
from typing import Callable

from reportlab.platypus import Paragraph, Spacer

from . import pdf_primitives as prim
from .mdblocks import parse_blocks
from .payload import ExportPayload
from .tables import tables_for


class UnsupportedExport(RuntimeError):
    """Raised when a task has no PDF renderer."""


def _render_blocks(md: str, width: float, *, highlight: bool = False) -> list:
    flow: list = []
    for block in parse_blocks(md):
        if block.kind == "heading":
            style = prim.H2_STYLE if block.level <= 2 else prim.H3_STYLE
            flow.append(Paragraph(prim.inline_rl(block.text), style))
        elif block.kind == "paragraph":
            flow.append(prim.rich_para(block.text, highlight=highlight))
        elif block.kind == "list":
            for n, item in enumerate(block.items):
                if block.ordered:
                    ordinal = (
                        block.markers[n]
                        if n < len(block.markers) and block.markers[n]
                        else str(n + 1)
                    )
                    bullet = f"{ordinal}."
                else:
                    bullet = "•"
                flow.append(
                    Paragraph(
                        prim.highlight_markers_rl(item)
                        if highlight
                        else prim.inline_rl(item),
                        prim.LIST_ITEM_STYLE,
                        bulletText=bullet,
                    )
                )
            flow.append(Spacer(1, 6))
        elif block.kind == "quote":
            flow.append(Paragraph(prim.inline_rl(block.text), prim.QUOTE_STYLE))
        elif block.kind == "code":
            flow.append(
                Paragraph(
                    f'<font face="Courier" size="8">'
                    f"{prim.inline_rl(block.text)}</font>",
                    prim.SMALL_STYLE,
                )
            )
        elif block.kind == "table" and block.table:
            flow.extend(prim.data_table(block.table, width, highlight=highlight))
            flow.append(Spacer(1, 8))
    return flow


# --------------------------------------------------------------------------
# Per-task renderers
# --------------------------------------------------------------------------
def render_prose(payload: ExportPayload, width: float) -> list:
    return [Paragraph("Answer", prim.H2_STYLE), *_render_blocks(
        payload.answer_markdown, width, highlight=payload.highlight_markers
    )]


def render_sectioned(payload: ExportPayload, width: float) -> list:
    # Phase 2b: in authority mode a memo carries [REMOVED — ...] markers
    # recording citations the tool deleted. Those must be as impossible to skim
    # past as a pleading's gap markers, so the memo renderer honours the
    # payload's highlight decision rather than assuming "memos never highlight".
    return [Paragraph("Memorandum", prim.H2_STYLE), *_render_blocks(
        payload.answer_markdown, width, highlight=payload.highlight_markers
    )]


def render_letter(payload: ExportPayload, width: float) -> list:
    return _render_blocks(
            payload.answer_markdown, width,
            highlight=payload.highlight_markers,
        )


def render_timeline(payload: ExportPayload, width: float) -> list:
    # No section heading: the title block already names the task, and a second
    # "Timeline" directly above the table reads as a formatting mistake.
    flow: list = []
    tables = tables_for(payload)
    if not tables:
        return flow + _render_blocks(payload.answer_markdown, width)
    for t in tables:
        flow.extend(prim.data_table(t, width))
        flow.append(Spacer(1, 8))
    return flow


def render_entities(payload: ExportPayload, width: float) -> list:
    flow: list = []
    tables = tables_for(payload)
    if not tables:
        return flow + _render_blocks(payload.answer_markdown, width)
    for t in tables:
        if t.title:
            flow.append(Paragraph(prim.inline_rl(t.title), prim.H3_STYLE))
        if t.rows:
            flow.extend(prim.data_table(t, width))
        else:
            flow.append(
                Paragraph(
                    prim.inline_rl(
                        t.note or "None found in the retrieved passages."
                    ),
                    prim.SMALL_STYLE,
                )
            )
        flow.append(Spacer(1, 8))
    return flow


def render_comparison(payload: ExportPayload, width: float) -> list:
    flow: list = []
    tables = tables_for(payload)
    if not tables:
        return flow + _render_blocks(payload.answer_markdown, width)
    for t in tables:
        flow.extend(prim.data_table(t, width))
        flow.append(Spacer(1, 8))
    return flow


def render_pleading(payload: ExportPayload, width: float) -> list:
    """Pleading BODY only — the banner and cover note belong to `build_pdf`.
    Gap markers are highlighted so a reviewer cannot skim past them."""
    return [
        Paragraph("Pleading draft", prim.H2_STYLE),
        *_render_blocks(payload.answer_markdown, width, highlight=True),
    ]


Renderer = Callable[[ExportPayload, float], list]

PDF_RENDERERS: dict[str, Renderer] = {
    "summarize": render_prose,
    "find_facts": render_prose,
    "draft_memo": render_sectioned,
    "draft_correspondence": render_letter,
    "timeline": render_timeline,
    "find_entities": render_entities,
    "compare_clauses": render_comparison,
    "draft_pleading": render_pleading,
}


def _needs_landscape(payload: ExportPayload) -> bool:
    """A comparison across more than three documents is unreadable at portrait
    width, and a crushed column is how a lawyer misreads a cell."""
    if payload.task != "compare_clauses":
        return False
    return max((len(t.headers) for t in tables_for(payload)), default=0) > 3


def build_pdf(payload: ExportPayload) -> bytes:
    renderer = PDF_RENDERERS.get(payload.task)
    if renderer is None:
        raise UnsupportedExport(f"No PDF renderer for task {payload.task!r}.")

    buf = io.BytesIO()
    doc, width = prim.make_doc(
        buf, payload, use_landscape=_needs_landscape(payload)
    )

    story: list = []
    # --- code-owned DRAFT machinery, head -------------------------------
    if payload.is_pleading:
        story.append(prim.draft_banner(width))
        story.append(Spacer(1, 14))

    story.extend(prim.title_block(payload))

    if payload.authority_mode:
        story.append(Spacer(1, 8))
        story.append(prim.authority_disclaimer_box(payload, width))
        story.append(Spacer(1, 10))

    if payload.is_pleading:
        story.append(Spacer(1, 6))
        story.append(Paragraph("Cover note", prim.H2_STYLE))
        story.append(prim.cover_note_box(width))
        story.append(Spacer(1, 14))

    for warning in list(payload.pleading_warnings) + list(payload.export_warnings):
        story.append(prim.warning_box(warning, width))
        story.append(Spacer(1, 8))

    story.extend(renderer(payload, width))

    # --- code-owned DRAFT machinery, foot -------------------------------
    if payload.is_pleading:
        story.append(Spacer(1, 14))
        story.append(prim.draft_banner(width))

    story.append(Spacer(1, 10))
    story.extend(prim.citations_flowables(payload))

    doc.build(story)
    return buf.getvalue()
