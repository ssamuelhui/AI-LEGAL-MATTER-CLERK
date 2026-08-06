from __future__ import annotations

import email
import logging
import re
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from html import unescape
from pathlib import Path

import bleach
import pypdf
import pytesseract
import tiktoken
from pdf2image import convert_from_path
from PIL import Image
from pydantic import BaseModel

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
    """One indexable unit of a source document.

    `locator` is the citation label echoed verbatim into the model's
    `[SOURCE: ...]` header and back into the final Citation. It is the single
    field that carries source-type-specific citation semantics through an
    otherwise type-agnostic pipeline: PDFs set it to "p.5", emails set it to
    "from Kevin Oskoui, 2024-05-11". See docs/ARCHITECTURE.md (locator rename).
    """

    source: str
    locator: str
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


def _chunk_tokens(
    text: str,
    chunk_tokens: int = CHUNK_TARGET_TOKENS,
    overlap_tokens: int = CHUNK_OVERLAP_TOKENS,
) -> list[str]:
    """Split one block of text into overlapping token windows.

    The shared core of both chunkers. Returns the decoded text of each window;
    the caller attaches the source and locator. Empty/whitespace-only input
    yields no windows.
    """
    tokens = _ENC.encode(text)
    if not tokens:
        return []
    pieces: list[str] = []
    start = 0
    while start < len(tokens):
        end = min(start + chunk_tokens, len(tokens))
        pieces.append(_ENC.decode(tokens[start:end]))
        if end == len(tokens):
            break
        start = end - overlap_tokens
    return pieces


def chunk_pages(
    pages: list[tuple[int, str]],
    source: str,
    ocr_pages: set[int] | list[int] | None = None,
    chunk_tokens: int = CHUNK_TARGET_TOKENS,
    overlap_tokens: int = CHUNK_OVERLAP_TOKENS,
) -> list[Chunk]:
    """Token-window chunking within each page.

    Chunks never span pages so every chunk maps cleanly to a single page
    citation — the cost of cross-page chunks is having to either lie about the
    page or attach a range, neither of which is acceptable under the citation
    discipline this project enforces.

    `ocr_pages` are the page numbers whose text came from the OCR fallback (as
    returned by `extract_pdf_pages`). Those pages get a "(OCR)" suffix on their
    locator — "p.3 (OCR)" — which then flows verbatim through the [SOURCE: ...]
    header, the model's echoed citation, and the final Citation, so a lawyer
    verifying against the source knows the snippet may carry OCR character
    errors and is not a byte-exact copy. Omitted/None marks nothing (the
    backward-compatible default: existing callers and native-text PDFs are
    unaffected).
    """
    ocr = set(ocr_pages or ())
    chunks: list[Chunk] = []
    for page_no, text in pages:
        locator = f"p.{page_no} (OCR)" if page_no in ocr else f"p.{page_no}"
        for piece in _chunk_tokens(text, chunk_tokens, overlap_tokens):
            chunks.append(Chunk(source=source, locator=locator, text=piece))
    return chunks


def chunk_email(
    body: str,
    source: str,
    locator: str,
    chunk_tokens: int = CHUNK_TARGET_TOKENS,
    overlap_tokens: int = CHUNK_OVERLAP_TOKENS,
) -> list[Chunk]:
    """Token-window chunking for an email body.

    Emails have no pages, so the whole body is chunked as one unit with the
    700-token window reused from PDFs. Every chunk carries the same email
    locator (e.g. "from Kevin Oskoui, 2024-05-11"), so a multi-window reply
    chain still collapses to a single citation downstream.
    """
    return [
        Chunk(source=source, locator=locator, text=piece)
        for piece in _chunk_tokens(body, chunk_tokens, overlap_tokens)
    ]


# --------------------------------------------------------------------------
# .eml ingestion (Outlook / generic RFC-822 email exports)
# --------------------------------------------------------------------------
class EmailMetadata(BaseModel):
    """Parsed header metadata for one .eml file, surfaced on the result page
    and used to build the email's citation locator."""

    sender_name: str          # human-readable display name, falling back to address
    sender_email: str
    to: str
    cc: str
    date_iso: str             # YYYY-MM-DD only, or "" if the Date header is unparseable
    subject: str
    message_id: str
    locator: str              # citation label, e.g. "from Kevin Oskoui, 2024-05-11"


def _html_to_text(html: str) -> str:
    """Strip an HTML email part down to readable plain text.

    bleach (already a project dependency) removes every tag; the remaining
    text nodes come back HTML-escaped, so we unescape entities and collapse
    the runs of blank lines that tag removal leaves behind.
    """
    stripped = bleach.clean(html, tags=[], attributes={}, strip=True)
    text = unescape(stripped)
    # Collapse 3+ newlines to a paragraph break and trim trailing spaces.
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _email_body(msg: EmailMessage) -> str:
    """Prefer the text/plain part; fall back to HTML stripped via bleach.

    `get_body` (policy.default) already skips attachment parts, so this never
    returns attachment bytes.
    """
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        return str(plain.get_content()).strip()
    html_part = msg.get_body(preferencelist=("html",))
    if html_part is not None:
        return _html_to_text(str(html_part.get_content()))
    return ""


def _attachment_names(msg: EmailMessage) -> list[str]:
    """Filenames of parts explicitly marked as attachments. Names only — the
    bytes are never read or processed (attachment handling is out of scope)."""
    names: list[str] = []
    for part in msg.walk():
        if part.get_content_disposition() == "attachment":
            name = part.get_filename()
            names.append(name if name else "(unnamed attachment)")
    return names


def _email_metadata(msg: EmailMessage) -> EmailMetadata:
    raw_from = str(msg.get("From", "") or "")
    name, addr = parseaddr(raw_from)
    sender_name = name.strip() or addr or raw_from
    sender_email = addr

    date_iso = ""
    date_hdr = msg.get("Date")
    if date_hdr:
        try:
            date_iso = parsedate_to_datetime(str(date_hdr)).date().isoformat()
        except (TypeError, ValueError):
            date_iso = ""

    if date_iso:
        locator = f"from {sender_name}, {date_iso}"
    else:
        locator = f"from {sender_name}"

    return EmailMetadata(
        sender_name=sender_name,
        sender_email=sender_email,
        to=str(msg.get("To", "") or ""),
        cc=str(msg.get("Cc", "") or ""),
        date_iso=date_iso,
        subject=str(msg.get("Subject", "") or ""),
        message_id=str(msg.get("Message-ID", "") or ""),
        locator=locator,
    )


def extract_email(eml_path: Path) -> tuple[EmailMetadata, str, list[str]]:
    """Parse an .eml file into (metadata, body_text, attachment_filenames).

    Uses only the stdlib `email` package with the modern `policy.default`
    parser. The body is the text/plain part when present, else the HTML part
    stripped to text. Attachments are listed by name but never read.
    """
    with eml_path.open("rb") as f:
        msg = email.message_from_binary_file(f, policy=policy.default)
    return _email_metadata(msg), _email_body(msg), _attachment_names(msg)
