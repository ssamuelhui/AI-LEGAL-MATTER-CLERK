"""Export of task results to lawyer-native formats (Day 4d).

Public surface:

    store_result(payload) -> token      cache a rendered result for ~30 minutes
    get_result(token)     -> payload    lookup only; never evicts
    list_export_formats(payload)        which buttons the result page shows
    build_payload(...)                  snapshot a PipelineResult for export
    generate(payload, fmt) -> (bytes, mimetype, filename)

`generate` is the single entry point the web layer calls, so the endpoint does
not need to know which module produces which format.
"""

from __future__ import annotations

import re

from .cache import clear, get_result, list_export_formats, stats, store_result, sweep
from .payload import EXCEL_TASKS, ExportPayload, build_payload

FORMATS = {
    "docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "docx",
    ),
    "pdf": ("application/pdf", "pdf"),
    "xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xlsx",
    ),
}


class UnsupportedFormat(RuntimeError):
    """Requested format is not available for this payload's task."""


def _safe_filename(payload: ExportPayload, ext: str) -> str:
    """A descriptive, filesystem-safe download name. Pleadings are prefixed
    DRAFT so the draft status is visible in a file listing and in the
    'recently downloaded' strip of a browser."""
    parts = [payload.matter_name or "matter-clerk", payload.task_label]
    stem = "-".join(p for p in parts if p)
    stem = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower() or "export"
    prefix = "DRAFT-" if payload.is_pleading else ""
    return f"{prefix}{stem}-{payload.generated_on()}.{ext}"


def generate(payload: ExportPayload, fmt: str) -> tuple[bytes, str, str]:
    """Render `payload` in `fmt`. Returns (bytes, mimetype, download filename)."""
    if fmt not in FORMATS:
        raise UnsupportedFormat(f"Unknown export format {fmt!r}.")
    if fmt == "xlsx" and payload.task not in EXCEL_TASKS:
        raise UnsupportedFormat(
            f"{payload.task_label} produces prose, not a table — "
            f"export it as Word or PDF instead."
        )

    if fmt == "docx":
        from .docx_render import build_docx

        data = build_docx(payload)
    elif fmt == "pdf":
        from .pdf_render import build_pdf

        data = build_pdf(payload)
    else:
        from .xlsx_render import build_xlsx

        data = build_xlsx(payload)

    mimetype, ext = FORMATS[fmt]
    return data, mimetype, _safe_filename(payload, ext)


__all__ = [
    "EXCEL_TASKS",
    "ExportPayload",
    "UnsupportedFormat",
    "build_payload",
    "clear",
    "generate",
    "get_result",
    "list_export_formats",
    "stats",
    "store_result",
    "sweep",
]
