from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pypdf
import pytesseract
import tiktoken
from pdf2image import convert_from_path
from PIL import Image

log = logging.getLogger("matter_clerk")

CHUNK_TARGET_TOKENS = 700
CHUNK_OVERLAP_TOKENS = 100

OCR_DPI = 300
OCR_TIMEOUT_SECONDS = 30
NEAR_WHITE_THRESHOLD = 240   # grayscale value at/above which a pixel counts as near-white
BLANK_FRACTION = 0.99        # fraction of near-white pixels above which a page reads as blank

_ENC = tiktoken.get_encoding("cl100k_base")


@dataclass
class Chunk:
    source: str
    page: int
    text: str


def _render_page(pdf_path: Path, page_no: int, dpi: int = OCR_DPI) -> Image.Image:
    images = convert_from_path(
        str(pdf_path), dpi=dpi, first_page=page_no, last_page=page_no
    )
    return images[0]


def _is_blank(img: Image.Image) -> bool:
    """True if >= BLANK_FRACTION of the rendered page is near-white.

    Uses PIL's O(256) histogram rather than per-pixel iteration so a full-page
    300 DPI image (~8 M pixels) classifies in milliseconds.
    """
    gray = img.convert("L")
    hist = gray.histogram()
    total = sum(hist)
    if total == 0:
        return True
    near_white = sum(hist[NEAR_WHITE_THRESHOLD:])
    return (near_white / total) >= BLANK_FRACTION


def extract_pdf_pages(
    pdf_path: Path,
) -> tuple[list[tuple[int, str]], list[int], list[int]]:
    """Extract text per page, falling back to OCR for pages pypdf cannot read.

    Returns:
        pages: (1-indexed page number, page text) for every page that produced
            text, whether the text came from pypdf or from OCR. The caller
            cannot tell the two sources apart from this list alone, by design —
            page-level citation accuracy is what matters.
        ocr_pages: page numbers whose text came from OCR (subset of `pages`).
        unreadable_pages: page numbers where neither pypdf nor OCR yielded text
            AND the page image was not effectively blank. Truly-blank pages
            (>= BLANK_FRACTION near-white at OCR_DPI) are silently dropped.

    System dependencies (must be on PATH): Tesseract OCR (`tesseract --version`)
    and Poppler (`pdftoppm -v`). The Python wrappers `pytesseract` and
    `pdf2image` shell out to these binaries; pip installs alone are not enough.
    """
    reader = pypdf.PdfReader(str(pdf_path))
    pages: list[tuple[int, str]] = []
    ocr_pages: list[int] = []
    unreadable_pages: list[int] = []

    for i, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            pages.append((i, text))
            continue

        log.info(f"page {i}: OCR'ing (pypdf returned no text)...")
        try:
            image = _render_page(pdf_path, i)
        except Exception as e:
            log.warning(f"page {i}: render for OCR failed ({e}); marking unreadable")
            unreadable_pages.append(i)
            continue

        try:
            ocr_text = pytesseract.image_to_string(
                image, timeout=OCR_TIMEOUT_SECONDS
            ).strip()
        except RuntimeError as e:
            log.warning(f"page {i}: OCR failed ({e}); marking unreadable")
            unreadable_pages.append(i)
            continue

        if ocr_text:
            pages.append((i, ocr_text))
            ocr_pages.append(i)
            continue

        # OCR returned nothing — classify blank vs unreadable from the rendered image.
        if _is_blank(image):
            log.info(f"page {i}: blank (silently dropped)")
        else:
            unreadable_pages.append(i)

    return pages, ocr_pages, unreadable_pages


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
