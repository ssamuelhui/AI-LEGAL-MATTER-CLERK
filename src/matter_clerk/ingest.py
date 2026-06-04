from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pypdf
import tiktoken

CHUNK_TARGET_TOKENS = 700
CHUNK_OVERLAP_TOKENS = 100

_ENC = tiktoken.get_encoding("cl100k_base")


@dataclass
class Chunk:
    source: str
    page: int
    text: str


def extract_pdf_pages(pdf_path: Path) -> tuple[list[tuple[int, str]], list[int]]:
    """Extract text per page from a native-text PDF.

    Returns a tuple of (pages, skipped_page_numbers). `pages` is a list of
    (page_number_1_based, page_text). Pages with no extractable text — image-only
    or scanned pages — are not included in `pages`; their page numbers are
    returned in `skipped_page_numbers` so the caller can surface that gap to the
    user. OCR is deferred to ING-2.
    """
    reader = pypdf.PdfReader(str(pdf_path))
    pages: list[tuple[int, str]] = []
    skipped: list[int] = []
    for i, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if not text:
            skipped.append(i)
            continue
        pages.append((i, text))
    return pages, skipped


def chunk_pages(
    pages: list[tuple[int, str]],
    source: str,
    chunk_tokens: int = CHUNK_TARGET_TOKENS,
    overlap_tokens: int = CHUNK_OVERLAP_TOKENS,
) -> list[Chunk]:
    """Token-window chunking within each page.

    Chunks never span pages so every chunk maps cleanly to a single page
    citation — the cost of cross-page chunks is having to either lie about the
    page or attach a range, neither of which is acceptable under the citation
    discipline this project enforces.
    """
    chunks: list[Chunk] = []
    for page_no, text in pages:
        tokens = _ENC.encode(text)
        if not tokens:
            continue
        start = 0
        while start < len(tokens):
            end = min(start + chunk_tokens, len(tokens))
            piece = _ENC.decode(tokens[start:end])
            chunks.append(Chunk(source=source, page=page_no, text=piece))
            if end == len(tokens):
                break
            start = end - overlap_tokens
    return chunks
