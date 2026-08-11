from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

import bleach
import markdown as md
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.serving import make_server

from . import audit, export, matters, pipeline, pleadings
from .prompts import (
    DEFAULT_TASK,
    available_tasks,
    get_template,
    missing_required_inputs,
    task_unavailable_reason,
)
from .vectorstore import connect, file_hash

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
        if field.type in ("multiselect", "file_multiselect"):
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
    # file_multiselect values are opaque file ids, and the files actually used are
    # already reported honestly by the "Compared across:" provenance line — so
    # showing raw ids here would be both unreadable and redundant.
    skip = {f.name for f in template.inputs if f.type == "file_multiselect"}
    out: dict = {}
    for name, val in structured_inputs.items():
        if name in skip:
            continue
        key = label_by_name.get(name, name)
        if isinstance(val, bool):
            val = "Yes" if val else "No"
        elif isinstance(val, list):
            val = ", ".join(val)
        out[key] = val
    return out


def _render_result(
    result, template, task, structured_inputs, back_url, back_label, matter_name=None
):
    """Render result.html. Shared by the ad-hoc and matter query paths; the only
    per-context difference is the back link."""
    is_pleading = task == "draft_pleading"
    is_compare = task == pipeline.COMPARE_TASK_ID
    provenance_label = "Compared across" if is_compare else "Drew on"
    party_role = (
        pleadings.role_for(structured_inputs.get("pleading_type"))
        if is_pleading
        else None
    )
    request_summary = _display_inputs(template, structured_inputs)

    # Day 4d: snapshot this result for export and hand the page its token. The
    # entry lives ~30 minutes and is NOT consumed by an export, so a lawyer can
    # take Word and then PDF from the same result page.
    payload = export.build_payload(
        result=result,
        template=template,
        task=task,
        structured_inputs=structured_inputs,
        request_summary=request_summary,
        party_role=party_role,
        provenance_label=provenance_label,
        matter_name=matter_name,
    )
    export_token = export.store_result(payload)

    return render_template(
        "result.html",
        is_compare=is_compare,
        export_token=export_token,
        export_formats=export.list_export_formats(payload),
        export_warnings=result.export_warnings,
        # "Drew on" asserts contribution, which is wrong for Compare Clauses:
        # its provenance list includes files that were searched and found to
        # lack the clause. That is a finding, so it gets honest wording.
        provenance_label=provenance_label,
        back_url=back_url,
        back_label=back_label,
        task_label=template.label,
        party_role=party_role,
        request_summary=request_summary,
        is_pleading=is_pleading,
        draft_banner=pleadings.DRAFT_BANNER if is_pleading else None,
        cover_note_html=(
            render_markdown(pleadings.COVER_NOTE) if is_pleading else None
        ),
        pleading_warnings=result.pleading_warnings,
        email_metadata=result.email_metadata,
        attachment_warnings=result.attachment_warnings,
        answer_html=render_markdown(result.answer),
        citations=result.citations,
        cross_document=result.cross_document,
        retrieved_sources=result.retrieved_sources,
        ocr_pages=result.ocr_pages,
        unreadable_pages=result.unreadable_pages,
        model=result.model,
        embed_model=result.embed_model,
        top_k=result.top_k,
        timestamp=result.timestamp,
        pdf_sha256=result.pdf_sha256,
    )


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB (whole request)
    # Random per-process key: enough to sign the flash cookie for this single-user
    # local tool. A restart invalidates outstanding flash cookies, which is fine.
    app.secret_key = os.urandom(24)

    # ----------------------------------------------------------------------
    # Render helpers (kept inside create_app so url_for is available)
    # ----------------------------------------------------------------------
    def render_ad_hoc(status=200, **kw):
        ok, err = _qdrant_ok()
        defaults = dict(
            qdrant_ok=ok, qdrant_err=err, error=None,
            # Matter-only tasks (Compare Clauses) never appear on the ad-hoc form.
            tasks=available_tasks(None), selected_task=DEFAULT_TASK,
            values={}, top_k="", reindex=False, limitation_signals=None,
            action=url_for("ad_hoc_query"),
        )
        defaults.update(kw)
        return render_template("ad_hoc.html", **defaults), status

    def render_matter_detail(conn, matter_id, status=200, **kw):
        matter = matters.get_matter(conn, matter_id)
        files = matters.list_files(conn, matter_id)
        queryable = [f for f in files if f.ingest_status == "ingested"]
        ok, err = _qdrant_ok()
        defaults = dict(
            qdrant_ok=ok, qdrant_err=err, error=None,
            matter=matter, files=files,
            queryable_files=queryable, matter_files=queryable,
            # Compare Clauses appears only once the matter holds 2+ ingested files.
            tasks=available_tasks(len(queryable)),
            compare_max_files=pipeline.COMPARE_MAX_FILES,
            selected_task=DEFAULT_TASK,
            values={}, top_k="", limitation_signals=None, limitation_by_file=None,
            selected_file_id=None,
            action=url_for("matter_query", matter_id=matter_id),
        )
        defaults.update(kw)
        return render_template("matter_detail.html", **defaults), status

    def _ingest_upload_into_matter(conn, matter, upload) -> None:
        """Ingest one uploaded file into the matter, flashing a per-file result.
        Never raises: a bad file in a multi-file batch must not abort the rest
        or 500 the request. Manifest row is inserted (and duplicate-checked by
        content hash) BEFORE any copy/ingest work; a failed ingest leaves a
        'failed' row with the error rather than a silent gap."""
        filename = upload.filename
        suffix = Path(filename).suffix.lower()
        if suffix not in (".pdf", ".eml"):
            flash(f"{filename}: unsupported file type (use .pdf or .eml).", "error")
            return

        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        tmp.close()
        tmp_path: Path | None = Path(tmp.name)
        try:
            upload.save(str(tmp_path))
            sha = file_hash(tmp_path)
            coll = matters.collection_name(matter.id, sha)
            stored = matters.stored_path_for(matter.id, sha, suffix)
            try:
                row = matters.add_file_pending(
                    conn, matter.id, filename,
                    "eml" if suffix == ".eml" else "pdf",
                    sha, coll, str(stored),
                )
            except matters.DuplicateFileInMatter as e:
                flash(str(e), "warn")
                return
            stored.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp_path), str(stored))
            tmp_path = None  # moved into the store; nothing left to clean up
            try:
                pipeline.ingest_file(stored, filename, collection=coll)
                matters.mark_file_ingested(conn, row.id)
                flash(f"Added {filename}.", "ok")
            except Exception as e:
                matters.mark_file_failed(conn, row.id, str(e))
                flash(f"{filename}: ingest failed: {e}", "error")
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    # ----------------------------------------------------------------------
    # Landing: matters list
    # ----------------------------------------------------------------------
    @app.get("/")
    def index():
        conn = matters.connect()
        try:
            ms = matters.list_matters(conn)
        finally:
            conn.close()
        ok, err = _qdrant_ok()
        return render_template(
            "matters.html", matters=ms, qdrant_ok=ok, qdrant_err=err, error=None
        )

    @app.post("/matters/new")
    def matter_new():
        name = (request.form.get("name") or "").strip()
        description = (request.form.get("description") or "").strip()
        conn = matters.connect()
        try:
            if not name:
                flash("Matter name is required.", "error")
                return redirect(url_for("index"))
            try:
                m = matters.create_matter(conn, name, description)
            except matters.DuplicateMatterName as e:
                flash(str(e), "error")
                return redirect(url_for("index"))
            flash(f"Created matter “{m.name}”.", "ok")
            return redirect(url_for("matter_detail", matter_id=m.id))
        finally:
            conn.close()

    # ----------------------------------------------------------------------
    # Matter detail + file upload + query
    # ----------------------------------------------------------------------
    @app.get("/matters/<int:matter_id>")
    def matter_detail(matter_id):
        conn = matters.connect()
        try:
            try:
                matters.get_matter(conn, matter_id)
            except matters.MatterNotFound:
                abort(404)
            return render_matter_detail(conn, matter_id)
        finally:
            conn.close()

    @app.post("/matters/<int:matter_id>/upload")
    def matter_upload(matter_id):
        conn = matters.connect()
        try:
            try:
                matter = matters.get_matter(conn, matter_id)
            except matters.MatterNotFound:
                abort(404)
            uploads = [u for u in request.files.getlist("files") if u and u.filename]
            if not uploads:
                flash("No files selected.", "error")
                return redirect(url_for("matter_detail", matter_id=matter_id))
            for up in uploads:
                _ingest_upload_into_matter(conn, matter, up)
            return redirect(url_for("matter_detail", matter_id=matter_id))
        finally:
            conn.close()

    @app.post("/matters/<int:matter_id>/query")
    def matter_query(matter_id):
        conn = matters.connect()
        try:
            try:
                matters.get_matter(conn, matter_id)
            except matters.MatterNotFound:
                abort(404)

            task = request.form.get("task") or DEFAULT_TASK
            top_k_raw = (request.form.get("top_k") or "").strip()
            try:
                top_k = int(top_k_raw) if top_k_raw else None
            except ValueError:
                top_k = None
            file_id_raw = (request.form.get("file_id") or "").strip()

            try:
                template = get_template(task)
            except KeyError:
                return render_matter_detail(
                    conn, matter_id, status=400, error=f"Unknown task: {task}."
                )

            structured_inputs = _collect_web_inputs(template, request.form)
            common = dict(
                selected_task=task, values=structured_inputs,
                top_k=top_k_raw, selected_file_id=file_id_raw,
            )

            # Server-side counterparts to the form's task filtering + JS gating
            # (Day 4c). The form hides Compare Clauses on a <2-file matter and
            # hides the single-file picker when it is selected; these catch a
            # POST that arrives regardless.
            n_ingested = len([
                f for f in matters.list_files(conn, matter_id)
                if f.ingest_status == "ingested"
            ])
            unavailable = task_unavailable_reason(task, n_ingested)
            if unavailable:
                return render_matter_detail(
                    conn, matter_id, status=400, error=unavailable, **common
                )
            if task == pipeline.COMPARE_TASK_ID and file_id_raw:
                return render_matter_detail(
                    conn, matter_id, status=400,
                    error="Compare Clauses runs across the documents in this "
                          "matter. Clear the single-file restriction, and use "
                          "“Compare across which files?” to narrow the "
                          "comparison instead.", **common
                )

            # Dispatch on file_id presence (Day 4b). Default (no file_id) queries
            # the whole matter via scatter-gather; an explicit file_id restricts
            # to that one file — today's single-collection path, unchanged.
            mf = None
            files = None
            if file_id_raw:
                # ---- single-file branch: resolve + authorize BEFORE any work. A
                # file_id that is non-integer, unknown, or from another matter is
                # a clear refusal, never a 500. (Behaviour preserved from Day 4a.)
                try:
                    file_id = int(file_id_raw)
                except (TypeError, ValueError):
                    return render_matter_detail(
                        conn, matter_id, status=400,
                        error="Please choose a file to query.", **common
                    )
                try:
                    mf = matters.get_file_in_matter(conn, matter_id, file_id)
                except matters.MatterError as e:  # FileNotInMatter / unknown id
                    return render_matter_detail(
                        conn, matter_id, status=400, error=str(e), **common
                    )
                if mf.ingest_status != "ingested":
                    return render_matter_detail(
                        conn, matter_id, status=400,
                        error=f"{mf.filename} is not successfully ingested "
                              f"(status: {mf.ingest_status}).", **common
                    )
            else:
                # ---- whole-matter branch: every successfully-ingested file ----
                files = [
                    f for f in matters.list_files(conn, matter_id)
                    if f.ingest_status == "ingested"
                ]
                if not files:
                    return render_matter_detail(
                        conn, matter_id, status=400,
                        error="This matter has no successfully ingested files to "
                              "query.", **common
                    )
                if task == pipeline.COMPARE_TASK_ID:
                    # Optional user subset. Authorize every submitted id against
                    # this matter exactly as the file_id path does — a tampered
                    # or foreign id is a refusal, never a 500 and never a silent
                    # drop. Column order follows the matter's file order, not the
                    # order the checkboxes happened to submit in.
                    picked_raw = structured_inputs.get("file_ids") or []
                    if picked_raw:
                        picked: set[int] = set()
                        for raw in picked_raw:
                            try:
                                fid = int(raw)
                            except (TypeError, ValueError):
                                return render_matter_detail(
                                    conn, matter_id, status=400,
                                    error="Invalid file selection.", **common
                                )
                            try:
                                sel = matters.get_file_in_matter(
                                    conn, matter_id, fid
                                )
                            except matters.MatterError as e:
                                return render_matter_detail(
                                    conn, matter_id, status=400,
                                    error=str(e), **common
                                )
                            if sel.ingest_status != "ingested":
                                return render_matter_detail(
                                    conn, matter_id, status=400,
                                    error=f"{sel.filename} is not successfully "
                                          f"ingested (status: "
                                          f"{sel.ingest_status}).", **common
                                )
                            picked.add(fid)
                        files = [f for f in files if f.id in picked]

            missing = missing_required_inputs(template, structured_inputs)
            if missing:
                return render_matter_detail(
                    conn, matter_id, status=400,
                    error=f"Please provide: {', '.join(missing)}.", **common
                )
            if task == "draft_pleading":
                pleading_errors = pleadings.validate_pleading_inputs(structured_inputs)
                if pleading_errors:
                    return render_matter_detail(
                        conn, matter_id, status=400,
                        error=" ".join(pleading_errors), **common
                    )

            try:
                if mf is not None:
                    result = pipeline.run_query(
                        pdf_path=Path(mf.stored_path),
                        source_name=mf.filename,
                        task=task,
                        structured_inputs=structured_inputs,
                        top_k=top_k,
                        reindex=False,            # already ingested; never re-ingest
                        collection=mf.collection,  # pinned -> single-collection
                        matter_id=matter_id,       # -> audit log
                    )
                elif task == pipeline.COMPARE_TASK_ID:
                    result = pipeline.run_compare_clauses(
                        files=files,
                        structured_inputs=structured_inputs,
                        matter_id=matter_id,
                        top_k=top_k,
                    )
                else:
                    result = pipeline.run_matter_query(
                        files=files,
                        task=task,
                        structured_inputs=structured_inputs,
                        matter_id=matter_id,
                        top_k=top_k,
                    )
            except pipeline.CompareClausesNotApplicable as e:
                return render_matter_detail(
                    conn, matter_id, status=400, error=str(e), **common
                )
            except pipeline.QdrantUnreachable as e:
                return render_matter_detail(
                    conn, matter_id, status=503,
                    qdrant_ok=False, qdrant_err=str(e), **common
                )
            except pipeline.PdfHasNoText as e:
                return render_matter_detail(
                    conn, matter_id, status=422, error=str(e), **common
                )
            except pipeline.LimitationReviewRequired as e:
                return render_matter_detail(
                    conn, matter_id, status=200,
                    limitation_signals=e.signals,
                    limitation_by_file=e.signals_by_file, **common
                )

            # Matter-context query audit (Day 4b): records which files grounded
            # the answer. Fires ONLY for the whole-matter branch — never for the
            # single-file branch (run_query has its own events) nor ad-hoc (no
            # matter_id). No query text / particulars: file IDs are the audit
            # signal and may not carry privileged content.
            if mf is None:
                audit.log_event(
                    "matter_query",
                    matter_id=matter_id,
                    task=task,
                    retrieved_file_ids=result.retrieved_file_ids,
                )

            matters.touch_last_queried(conn, matter_id)
            return _render_result(
                result, template, task, structured_inputs,
                back_url=url_for("matter_detail", matter_id=matter_id),
                back_label="Back to matter",
                matter_name=matters.get_matter(conn, matter_id).name,
            )
        finally:
            conn.close()

    # ----------------------------------------------------------------------
    # Export (Day 4d)
    # ----------------------------------------------------------------------
    @app.get("/export/<token>/<fmt>")
    def export_result(token, fmt):
        """Generate one file from a cached result.

        The token identifies the task, so the format is the only other thing the
        URL needs. An expired token is the COMMON case (30-minute TTL), not an
        error condition, so it renders a short explanatory page with a 410
        rather than a bare 404.
        """
        payload = export.get_result(token)
        if payload is None:
            return (
                render_template("export_expired.html"),
                410,
            )
        try:
            data, mimetype, filename = export.generate(payload, fmt)
        except export.UnsupportedFormat as e:
            return render_template("export_error.html", message=str(e)), 400
        except Exception as e:  # generation failure must not 500 opaquely
            log.exception("Export generation failed")
            return (
                render_template(
                    "export_error.html",
                    message=f"The {fmt.upper()} file could not be generated: {e}",
                ),
                500,
            )

        response = make_response(data)
        response.headers["Content-Type"] = mimetype
        response.headers["Content-Disposition"] = (
            f'attachment; filename="{filename}"'
        )
        response.headers["Content-Length"] = str(len(data))
        # A matter document must not sit in a shared/proxy cache.
        response.headers["Cache-Control"] = "no-store"
        return response

    # ----------------------------------------------------------------------
    # Ad-hoc single-file path (today's behavior, preserved)
    # ----------------------------------------------------------------------
    @app.get("/ad-hoc")
    def ad_hoc():
        return render_ad_hoc()

    @app.post("/ad-hoc/query")
    def ad_hoc_query():
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
            return render_ad_hoc(
                status=400, error=f"Unknown task: {task}.",
                top_k=top_k_raw, reindex=reindex,
            )

        structured_inputs = _collect_web_inputs(template, request.form)
        common = dict(
            selected_task=task, values=structured_inputs,
            top_k=top_k_raw, reindex=reindex,
        )

        # Matter-only tasks are absent from this form's dropdown; refuse cleanly
        # if one is posted anyway (Day 4c).
        unavailable = task_unavailable_reason(task, None)
        if unavailable:
            return render_ad_hoc(status=400, error=unavailable, **common)

        if not upload or not upload.filename:
            return render_ad_hoc(
                status=400, error="Please choose a PDF or .eml file.", **common
            )
        suffix = Path(upload.filename).suffix.lower()
        if suffix not in (".pdf", ".eml"):
            return render_ad_hoc(
                status=400,
                error="Unsupported file type. Upload a .pdf or .eml file.",
                **common,
            )

        missing = missing_required_inputs(template, structured_inputs)
        if missing:
            return render_ad_hoc(
                status=400, error=f"Please provide: {', '.join(missing)}.", **common
            )
        if task == "draft_pleading":
            pleading_errors = pleadings.validate_pleading_inputs(structured_inputs)
            if pleading_errors:
                return render_ad_hoc(status=400, error=" ".join(pleading_errors), **common)

        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
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
                    # matter_id defaults to None -> audit records null for ad-hoc.
                )
            except pipeline.QdrantUnreachable as e:
                return render_ad_hoc(status=503, qdrant_ok=False, qdrant_err=str(e), **common)
            except pipeline.PdfHasNoText as e:
                return render_ad_hoc(status=422, error=str(e), **common)
            except pipeline.LimitationReviewRequired as e:
                return render_ad_hoc(status=200, limitation_signals=e.signals, **common)
        finally:
            tmp_path.unlink(missing_ok=True)

        return _render_result(
            result, template, task, structured_inputs,
            back_url=url_for("ad_hoc"), back_label="Run another task",
        )

    return app


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    # qdrant-client issues every request through httpx, whose INFO logger prints
    # "HTTP Request: GET http://localhost:6333/... 200 OK" for each call — demo
    # noise. Quiet it to WARNING so successful calls are silent but failures
    # still surface.
    logging.getLogger("httpx").setLevel(logging.WARNING)
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
