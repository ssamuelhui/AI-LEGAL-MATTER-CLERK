from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import bleach
import markdown as md
from dotenv import load_dotenv
from flask import Flask, render_template, request
from werkzeug.serving import make_server

from . import pipeline, pleadings
from .prompts import (
    DEFAULT_TASK,
    get_template,
    missing_required_inputs,
    ordered_templates,
)
from .vectorstore import connect

log = logging.getLogger("matter_clerk.web")

ALLOWED_TAGS = [
    "p", "br", "hr", "em", "strong", "code", "pre",
    "ul", "ol", "li", "blockquote",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "th", "td",
]
ALLOWED_ATTRS: dict[str, list[str]] = {}

_MD = md.Markdown(extensions=["tables", "fenced_code"])


def render_markdown(text: str) -> str:
    raw_html = _MD.reset().convert(text)
    return bleach.clean(
        raw_html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, strip=True
    )


def _qdrant_ok() -> tuple[bool, str | None]:
    try:
        host = os.environ.get("QDRANT_HOST", "localhost")
        port = int(os.environ.get("QDRANT_PORT", "6333"))
        client = connect(host, port)
        client.get_collections()
        return True, None
    except Exception as e:
        return False, str(e)


def _collect_web_inputs(template, form) -> dict:
    """Pull this task's declared inputs out of the submitted form."""
    inputs: dict = {}
    for field in template.inputs:
        if field.type == "multiselect":
            vals = form.getlist(field.name)
            if vals:
                inputs[field.name] = vals
        elif field.type == "checkbox":
            if form.get(field.name):
                inputs[field.name] = True
        else:
            val = (form.get(field.name) or "").strip()
            if val:
                inputs[field.name] = val
    return inputs


def _display_inputs(template, structured_inputs: dict) -> dict:
    """Map raw input names/values to friendly labels/strings for the result page."""
    label_by_name = {f.name: f.label for f in template.inputs}
    out: dict = {}
    for name, val in structured_inputs.items():
        key = label_by_name.get(name, name)
        if isinstance(val, bool):
            val = "Yes" if val else "No"
        elif isinstance(val, list):
            val = ", ".join(val)
        out[key] = val
    return out


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB

    def render_index(status=200, **kw):
        defaults = dict(
            qdrant_ok=True,
            qdrant_err=None,
            tasks=ordered_templates(),
            selected_task=DEFAULT_TASK,
            values={},
            top_k="",
            reindex=False,
            limitation_signals=None,
        )
        defaults.update(kw)
        return render_template("index.html", **defaults), status

    @app.get("/")
    def index():
        ok, err = _qdrant_ok()
        return render_index(qdrant_ok=ok, qdrant_err=err)[0]

    @app.post("/ask")
    def ask():
        upload = request.files.get("pdf")
        task = request.form.get("task") or DEFAULT_TASK
        top_k_raw = (request.form.get("top_k") or "").strip()
        try:
            top_k = int(top_k_raw) if top_k_raw else None
        except ValueError:
            top_k = None
        reindex = bool(request.form.get("reindex"))

        try:
            template = get_template(task)
        except KeyError:
            return render_index(
                status=400, error=f"Unknown task: {task}.", top_k=top_k_raw,
                reindex=reindex,
            )

        structured_inputs = _collect_web_inputs(template, request.form)
        common = dict(
            selected_task=task, values=structured_inputs,
            top_k=top_k_raw, reindex=reindex,
        )

        if not upload or not upload.filename:
            return render_index(status=400, error="Please choose a PDF.", **common)

        missing = missing_required_inputs(template, structured_inputs)
        if missing:
            return render_index(
                status=400,
                error=f"Please provide: {', '.join(missing)}.",
                **common,
            )

        if task == "draft_pleading":
            pleading_errors = pleadings.validate_pleading_inputs(structured_inputs)
            if pleading_errors:
                return render_index(
                    status=400, error=" ".join(pleading_errors), **common
                )

        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp.close()
        tmp_path = Path(tmp.name)
        try:
            upload.save(str(tmp_path))
            try:
                result = pipeline.run_query(
                    pdf_path=tmp_path,
                    source_name=upload.filename,
                    task=task,
                    structured_inputs=structured_inputs,
                    top_k=top_k,
                    reindex=reindex,
                )
            except pipeline.QdrantUnreachable as e:
                return render_index(status=503, qdrant_ok=False, qdrant_err=str(e),
                                    **common)
            except pipeline.PdfHasNoText as e:
                return render_index(status=422, error=str(e), **common)
            except pipeline.LimitationReviewRequired as e:
                return render_index(status=200, limitation_signals=e.signals, **common)
        finally:
            tmp_path.unlink(missing_ok=True)

        is_pleading = task == "draft_pleading"
        return render_template(
            "result.html",
            task_label=template.label,
            party_role=(
                pleadings.role_for(structured_inputs.get("pleading_type"))
                if is_pleading
                else None
            ),
            request_summary=_display_inputs(template, structured_inputs),
            is_pleading=is_pleading,
            draft_banner=pleadings.DRAFT_BANNER if is_pleading else None,
            cover_note_html=(
                render_markdown(pleadings.COVER_NOTE) if is_pleading else None
            ),
            pleading_warnings=result.pleading_warnings,
            answer_html=render_markdown(result.answer),
            citations=result.citations,
            ocr_pages=result.ocr_pages,
            unreadable_pages=result.unreadable_pages,
            model=result.model,
            embed_model=result.embed_model,
            top_k=result.top_k,
            timestamp=result.timestamp,
            pdf_sha256=result.pdf_sha256,
        )

    return app


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    load_dotenv()

    host = "127.0.0.1"
    port = int(os.environ.get("MATTER_CLERK_PORT", "5050"))
    app = create_app()

    server = make_server(host, port, app, threaded=True)
    # In-flight requests must run to completion on Ctrl+C so the tmp-file
    # `finally` clause executes. Otherwise daemon worker threads would be
    # killed without unwinding `finally`, leaking the upload tmp file.
    server.daemon_threads = False

    print(f"Matter Clerk listening at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("Shutting down (waiting for any in-flight requests to finish)...")
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
