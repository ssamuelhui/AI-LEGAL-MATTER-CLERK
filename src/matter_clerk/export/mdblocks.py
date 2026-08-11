"""Block-level markdown walker shared by the Word and PDF generators (Day 4d).

The per-task renderers own layout decisions; they should not each re-derive
"is this line a heading, a table row, or a list item". This module answers that
once and hands back typed blocks.

Scope is intentionally the subset the task templates actually instruct the model
to emit — headings, paragraphs, pipe tables, ordered/unordered lists, block
quotes and fenced code. It is not a general markdown implementation, and it does
not need to be: the web UI keeps using `markdown` + `bleach`, and this exists so
the exports can lay the same content out natively rather than through HTML.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from .tables import ExportTable, parse_markdown_tables

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_ULI = re.compile(r"^\s{0,3}[-*+]\s+(.*)$")
_OLI = re.compile(r"^\s{0,3}(\d+)[.)]\s+(.*)$")
_SEP = re.compile(r"^\s*\|?[\s:\-|]+\|[\s:\-|]*$")
_FENCE = re.compile(r"^\s*```")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")


class Block(BaseModel):
    kind: Literal["heading", "paragraph", "table", "list", "quote", "code"]
    level: int = 0
    text: str = ""
    items: list[str] = Field(default_factory=list)
    ordered: bool = False
    # The ORIGINAL ordinal of each ordered-list item ("1", "2", "14"), never a
    # re-derived sequence. Pleading paragraph numbers are legally significant —
    # a defence or a motion cross-references "paragraph 7 of the Claim" — so a
    # renderer must reproduce the model's numbering rather than renumbering from
    # one. Empty for unordered lists.
    markers: list[str] = Field(default_factory=list)
    table: ExportTable | None = None


def _is_block_start(line: str) -> bool:
    """Whether a line begins a new block, and so cannot be a lazy continuation
    of the list item above it."""
    return bool(
        _HEADING.match(line)
        or _ULI.match(line)
        or _OLI.match(line)
        or _FENCE.match(line)
        or _QUOTE.match(line)
        or line.strip().startswith("|")
    )


def parse_blocks(md: str) -> list[Block]:
    lines = (md or "").splitlines()
    blocks: list[Block] = []
    para: list[str] = []

    def flush_para() -> None:
        if para:
            blocks.append(Block(kind="paragraph", text=" ".join(para).strip()))
            para.clear()

    i = 0
    while i < len(lines):
        line = lines[i]

        if _FENCE.match(line):
            flush_para()
            body: list[str] = []
            i += 1
            while i < len(lines) and not _FENCE.match(lines[i]):
                body.append(lines[i])
                i += 1
            i += 1
            blocks.append(Block(kind="code", text="\n".join(body)))
            continue

        if not line.strip():
            flush_para()
            i += 1
            continue

        if m := _HEADING.match(line):
            flush_para()
            blocks.append(
                Block(kind="heading", level=len(m.group(1)), text=m.group(2).strip())
            )
            i += 1
            continue

        # Pipe table: a header row followed by a --- separator.
        if line.strip().startswith("|") and i + 1 < len(lines) and _SEP.match(lines[i + 1]):
            flush_para()
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                j += 1
            chunk = "\n".join(lines[i:j])
            parsed = parse_markdown_tables(chunk)
            if parsed:
                blocks.append(Block(kind="table", table=parsed[0]))
            i = j
            continue

        if _QUOTE.match(line):
            flush_para()
            items: list[str] = []
            while i < len(lines) and (m := _QUOTE.match(lines[i])):
                items.append(m.group(1).strip())
                i += 1
            blocks.append(Block(kind="quote", text=" ".join(items).strip()))
            continue

        if _ULI.match(line) or _OLI.match(line):
            flush_para()
            ordered = bool(_OLI.match(line))
            items = []
            markers: list[str] = []
            while i < len(lines):
                mu, mo = _ULI.match(lines[i]), _OLI.match(lines[i])
                if ordered and mo:
                    items.append(mo.group(2).strip())
                    markers.append(mo.group(1))  # preserve the source ordinal
                elif not ordered and mu:
                    items.append(mu.group(1).strip())
                    markers.append("")
                elif lines[i].strip() and items and not _is_block_start(lines[i]):
                    # Lazy continuation: markdown allows a wrapped item to
                    # continue on an unindented line. Legal prose is routinely
                    # hard-wrapped, and treating the remainder as a new
                    # paragraph splits a numbered pleading paragraph in two —
                    # which also splits any [ELEMENTS REQUIRED ...] marker
                    # across blocks and defeats its highlighting.
                    items[-1] += " " + lines[i].strip()
                else:
                    break
                i += 1
            blocks.append(
                Block(kind="list", items=items, ordered=ordered, markers=markers)
            )
            continue

        para.append(line.strip())
        i += 1

    flush_para()
    return blocks


# --------------------------------------------------------------------------
# Inline formatting
# --------------------------------------------------------------------------
# UNDERSCORE EMPHASIS IS DELIBERATELY NOT SUPPORTED.
#
# Source filenames routinely contain underscores, and every citation embeds one
# verbatim: "[Imperial_Plaza_Lease.pdf p.1]". Treating _..._ as italic renders
# that as "[ImperialPlazaLease.pdf p.1]" — silently corrupting the citation
# label a lawyer uses to find the passage in the source document, and breaking
# string-equality with the inline cite in the body. Asterisk emphasis is what
# the templates actually instruct the model to emit, so nothing is lost.
_INLINE = re.compile(r"(\*\*.+?\*\*|\*[^*\n]+?\*|`[^`\n]+?`)", re.DOTALL)


def inline_runs(text: str) -> list[tuple[str, bool, bool, bool]]:
    """Split into (text, bold, italic, code) runs for python-docx."""
    runs: list[tuple[str, bool, bool, bool]] = []
    for part in _INLINE.split(text or ""):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            runs.append((part[2:-2], True, False, False))
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            runs.append((part[1:-1], False, False, True))
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            runs.append((part[1:-1], False, True, False))
        else:
            runs.append((part, False, False, False))
    return runs


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def inline_rl(text: str) -> str:
    """Render inline markdown as ReportLab's mini-markup, XML-escaping first so
    a literal '<' in a legal document cannot break the paragraph parser."""
    out: list[str] = []
    for body, bold, italic, code in inline_runs(text):
        esc = _escape(body)
        if bold:
            esc = f"<b>{esc}</b>"
        if italic:
            esc = f"<i>{esc}</i>"
        if code:
            esc = f'<font face="Courier">{esc}</font>'
        out.append(esc)
    return "".join(out)
