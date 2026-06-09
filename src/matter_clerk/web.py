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

from . import pipeline
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


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB

    @app.get("/")
    def index():
        ok, err = _qdrant_ok()
        return render_template("index.html", qdrant_ok=ok, qdrant_err=err)

    @app.post("/ask")
    def ask():
        upload = request.files.get("pdf")
        question = (request.form.get("question") or "").strip()
        try:
            top_k = int(request.form.get("top_k") or 8)
        except ValueError:
            top_k = 8
        reindex = bool(request.form.get("reindex"))

        if not upload or not upload.filename:
            return render_template(
                "index.html", qdrant_ok=True, qdrant_err=None,
                error="Please choose a PDF.",
                question=question, top_k=top_k, reindex=reindex,
            ), 400
        if not question:
            return render_template(
                "index.html", qdrant_ok=True, qdrant_err=None,
                error="Please enter a question.",
                question="", top_k=top_k, reindex=reindex,
            ), 400

        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp.close()
        tmp_path = Path(tmp.name)
        try:
            upload.save(str(tmp_path))
            try:
                result = pipeline.run_query(
                    pdf_path=tmp_path,
                    source_name=upload.filename,
                    question=question,
                    top_k=top_k,
                    reindex=reindex,
                )
            except pipeline.QdrantUnreachable as e:
                return render_template(
                    "index.html", qdrant_ok=False, qdrant_err=str(e),
                    question=question, top_k=top_k, reindex=reindex,
                ), 503
            except pipeline.PdfHasNoText as e:
                return render_template(
                    "index.html", qdrant_ok=True, qdrant_err=None,
                    error=str(e),
                    question=question, top_k=top_k, reindex=reindex,
                ), 422
        finally:
            tmp_path.unlink(missing_ok=True)

        return render_template(
            "result.html",
            question=question,
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
