"""Low-level ReportLab building blocks (Day 4d).

Mirrors `docx_primitives` deliberately: the same named concepts (draft banner,
cover note box, data table, citations, metadata footer) so the two formats stay
recognisably the same document and a change of intent is made in two obvious
places rather than hunted for.
"""

from __future__ import annotations

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from .. import pleadings
from .mdblocks import inline_rl
from .payload import ExportPayload
from .tables import ExportTable

DRAFT_RED = colors.HexColor("#7A0000")
DRAFT_DARK = colors.HexColor("#4D0000")
DRAFT_TINT = colors.HexColor("#FDF3F3")
HEADER_FILL = colors.HexColor("#F2F2F2")
GRID = colors.HexColor("#999999")
MUTED = colors.HexColor("#666666")

_ss = getSampleStyleSheet()

BANNER_STYLE = ParagraphStyle(
    "MCBanner", parent=_ss["Normal"], fontName="Helvetica-Bold", fontSize=10.5,
    leading=13, textColor=colors.white, alignment=TA_CENTER,
)
TITLE_STYLE = ParagraphStyle(
    "MCTitle", parent=_ss["Normal"], fontName="Helvetica-Bold", fontSize=15,
    leading=19, spaceAfter=3,
)
SUB_STYLE = ParagraphStyle(
    "MCSub", parent=_ss["Normal"], fontSize=9, leading=12,
    textColor=MUTED, spaceAfter=10,
)
H2_STYLE = ParagraphStyle(
    "MCH2", parent=_ss["Normal"], fontName="Helvetica-Bold", fontSize=12,
    leading=15, spaceBefore=14, spaceAfter=6,
)
H3_STYLE = ParagraphStyle(
    "MCH3", parent=_ss["Normal"], fontName="Helvetica-Bold", fontSize=10.5,
    leading=13, spaceBefore=10, spaceAfter=4,
)
BODY_STYLE = ParagraphStyle(
    "MCBody", parent=_ss["Normal"], fontSize=10.5, leading=14.5, spaceAfter=7,
    alignment=TA_LEFT,
)
SMALL_STYLE = ParagraphStyle(
    "MCSmall", parent=_ss["Normal"], fontSize=9, leading=12, spaceAfter=4,
)
CELL_STYLE = ParagraphStyle(
    "MCCell", parent=_ss["Normal"], fontSize=8.5, leading=11,
)
CELL_HEAD_STYLE = ParagraphStyle(
    "MCCellHead", parent=CELL_STYLE, fontName="Helvetica-Bold",
)
COVER_INTRO_STYLE = ParagraphStyle(
    "MCCoverIntro", parent=_ss["Normal"], fontName="Helvetica-Bold", fontSize=9.5,
    leading=13, textColor=DRAFT_RED, spaceAfter=7,
)
COVER_ITEM_STYLE = ParagraphStyle(
    "MCCoverItem", parent=_ss["Normal"], fontSize=9, leading=12.5,
    leftIndent=15, bulletIndent=2, spaceAfter=5,
)
LIST_ITEM_STYLE = ParagraphStyle(
    "MCListItem", parent=_ss["Normal"], fontSize=10.5, leading=14.5,
    leftIndent=22, bulletIndent=4, spaceAfter=7,
)
CITE_STYLE = ParagraphStyle(
    "MCCite", parent=_ss["Normal"], fontSize=8.5, leading=11.5,
    leftIndent=16, firstLineIndent=-16, spaceAfter=3,
)
QUOTE_STYLE = ParagraphStyle(
    "MCQuote", parent=BODY_STYLE, leftIndent=18, textColor=colors.HexColor("#555555"),
    fontName="Helvetica-Oblique",
)


def highlight_markers_rl(text: str) -> str:
    """Wrap [ELEMENTS REQUIRED ...] / [ADDITIONAL MATERIAL REQUIRED ...] markers
    in a yellow-backed bold red span so an unfilled gap is unmissable in a PDF
    that may be forwarded to a client or opposing counsel."""
    parts: list[str] = []
    for segment, is_marker in pleadings.split_required_markers(text):
        if is_marker:
            parts.append(
                f'<font backColor="#FFF176" color="#7A0000"><b>'
                f"{inline_rl(segment)}</b></font>"
            )
        else:
            parts.append(inline_rl(segment))
    return "".join(parts)


def rich_para(text: str, style=BODY_STYLE, *, highlight: bool = False) -> Paragraph:
    return Paragraph(
        highlight_markers_rl(text) if highlight else inline_rl(text), style
    )


def draft_banner(width: float) -> Table:
    """Full-width dark-red band, matching the web UI's `.banner.draft`."""
    t = Table([[Paragraph(pleadings.DRAFT_BANNER, BANNER_STYLE)]], colWidths=[width])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), DRAFT_RED),
                ("BOX", (0, 0), (-1, -1), 1, DRAFT_DARK),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return t


def cover_note_box(width: float) -> KeepTogether:
    """The four SoW 1.4.3 points in a 2pt-bordered, tinted box.

    KeepTogether so the note never splits across a page break — a cover note
    whose first half sits alone on a page is exactly how a reader skips the
    remaining points.
    """
    import re

    intro, *rest = re.split(r"\n(?=1\.\s)", pleadings.COVER_NOTE, maxsplit=1)
    flow = [Paragraph(inline_rl(" ".join(intro.split())), COVER_INTRO_STYLE)]
    if rest:
        for chunk in re.split(r"\n(?=\d\.\s)", rest[0]):
            if not chunk.strip():
                continue
            num, _, body = chunk.partition(". ")
            flow.append(
                Paragraph(
                    inline_rl(" ".join(body.split())),
                    COVER_ITEM_STYLE,
                    bulletText=f"{num.strip()}.",
                )
            )
    t = Table([[flow]], colWidths=[width])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), DRAFT_TINT),
                ("BOX", (0, 0), (-1, -1), 2, DRAFT_RED),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    return KeepTogether(t)


def warning_box(text: str, width: float) -> Table:
    t = Table([[Paragraph(inline_rl(text), SMALL_STYLE)]], colWidths=[width])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFF8E1")),
                ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor("#F0D488")),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    return t


def data_table(table: ExportTable, width: float, *, highlight: bool = False) -> list:
    """An ExportTable as a bordered ReportLab table with a repeating header row.

    Every cell is a Paragraph so long legal text wraps instead of overflowing,
    and `repeatRows=1` re-prints the header on each page — a multi-page timeline
    whose columns are unlabelled after page 1 is unusable.
    """
    if not table.headers:
        return []
    n = len(table.headers)
    header = [Paragraph(inline_rl(h), CELL_HEAD_STYLE) for h in table.headers]
    body = [
        [
            Paragraph(
                highlight_markers_rl(c) if highlight else inline_rl(c), CELL_STYLE
            )
            for c in (row + [""] * n)[:n]
        ]
        for row in table.rows
    ]
    # First column carries the row label (attribute / date) and earns extra
    # width; the rest divide what is left evenly.
    if n > 2:
        first = width * min(0.28, 1.6 / n)
        widths = [first] + [(width - first) / (n - 1)] * (n - 1)
    else:
        widths = [width / n] * n

    t = Table([header] + body, colWidths=widths, repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, GRID),
                ("BACKGROUND", (0, 0), (-1, 0), HEADER_FILL),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    out: list = [t]
    if table.note:
        out.append(Spacer(1, 4))
        out.append(
            Paragraph(
                f'<font color="#666666" size="8"><i>{inline_rl(table.note)}</i></font>',
                SMALL_STYLE,
            )
        )
    return out


def title_block(payload: ExportPayload) -> list:
    flow: list = [Paragraph(inline_rl(payload.task_label), TITLE_STYLE)]
    bits: list[str] = []
    if payload.matter_name:
        bits.append(f"Matter: {payload.matter_name}")
    if payload.party_role:
        bits.append(f"Party role: {payload.party_role}")
    bits.append("matter-only")
    # A literal middle-dot, not the "&middot;" entity: inline_rl XML-escapes its
    # input, so an entity would render as visible markup.
    flow.append(Paragraph(inline_rl("  ·  ".join(bits)), SUB_STYLE))
    for key, value in payload.request_summary.items():
        flow.append(
            Paragraph(f"<b>{inline_rl(key)}:</b> {inline_rl(value)}", SMALL_STYLE)
        )
    return flow


def citations_flowables(payload: ExportPayload) -> list:
    flow: list = [Paragraph("Citations", H2_STYLE)]
    if payload.source_files:
        flow.append(
            Paragraph(
                f'<font color="#666666">{payload.provenance_label}: '
                f"{inline_rl(', '.join(payload.source_files))}</font>",
                SMALL_STYLE,
            )
        )
    if not payload.citations:
        flow.append(Paragraph("<i>No citations were recorded.</i>", SMALL_STYLE))
        return flow
    for n, c in enumerate(payload.citations, start=1):
        flow.append(
            Paragraph(
                f'{n}. <font face="Courier"><b>{inline_rl(c.inline())}</b></font> '
                f"&nbsp;<i>&ldquo;{inline_rl(c.text_snippet)}&rdquo;</i>",
                CITE_STYLE,
            )
        )
    return flow


# --------------------------------------------------------------------------
# Page furniture
# --------------------------------------------------------------------------
def _draw_watermark(canv, page_w: float, page_h: float) -> None:
    """Diagonal DRAFT watermark, drawn on EVERY page beneath the content.

    Deliberately part of the page template rather than a flowable in the story:
    it is not content, so it cannot be removed by deleting content.
    """
    canv.saveState()
    canv.setFillColor(DRAFT_RED)
    canv.setFillAlpha(0.09)
    canv.translate(page_w / 2, page_h / 2)
    canv.rotate(45)
    canv.setFont("Helvetica-Bold", 62)
    canv.drawCentredString(0, 8, "DRAFT")
    canv.setFont("Helvetica-Bold", 30)
    canv.drawCentredString(0, -34, "NOT FOR FILING")
    canv.restoreState()


def _draw_footer(canv, doc, payload: ExportPayload, page_w: float) -> None:
    canv.saveState()
    y = 0.58 * inch
    if payload.is_pleading:
        canv.setFillColor(DRAFT_RED)
        canv.setFont("Helvetica-Bold", 7.5)
        canv.drawCentredString(page_w / 2, y, pleadings.DRAFT_BANNER)
        y -= 10

    # Wrap the attribution on WORD boundaries within the text column, and keep
    # it clear of the page number. The footer is a required disclosure, so it
    # has to stay readable rather than run under the page number or off-page.
    canv.setFillColor(MUTED)
    canv.setFont("Helvetica", 7)
    avail = page_w - 2.4 * inch  # leave room for the page number at the right
    words = payload.attribution().split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if canv.stringWidth(trial, "Helvetica", 7) <= avail:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)

    for line in lines[:3]:
        canv.drawCentredString(page_w / 2, y, line)
        y -= 8.5

    # Below every attribution line, so the two can never overlap.
    canv.setFont("Helvetica", 7.5)
    canv.drawRightString(page_w - 0.9 * inch, y, f"Page {doc.page}")
    canv.restoreState()


def make_doc(buffer, payload: ExportPayload, *, use_landscape: bool = False):
    """A BaseDocTemplate whose single PageTemplate carries the watermark and
    footer callbacks. Returns (doc, content_width)."""
    pagesize = landscape(letter) if use_landscape else letter
    page_w, page_h = pagesize

    title = (
        f"DRAFT - NOT FOR FILING - {payload.task_label}"
        if payload.is_pleading
        else f"{payload.task_label} - Matter Clerk"
    )
    doc = BaseDocTemplate(
        buffer,
        pagesize=pagesize,
        leftMargin=0.9 * inch,
        rightMargin=0.9 * inch,
        topMargin=0.8 * inch,
        bottomMargin=0.95 * inch,
        title=title,
        author="Matter Clerk (automated draft)",
        subject=(
            "DRAFT pleading - not reviewed by counsel - not for filing or service"
            if payload.is_pleading
            else payload.attribution()
        ),
    )

    def on_page(canv, doc_):
        if payload.is_pleading:
            _draw_watermark(canv, page_w, page_h)
        _draw_footer(canv, doc_, payload, page_w)

    frame = Frame(
        doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body"
    )
    doc.addPageTemplates(
        [PageTemplate(id="main", frames=[frame], onPage=on_page)]
    )
    return doc, doc.width
