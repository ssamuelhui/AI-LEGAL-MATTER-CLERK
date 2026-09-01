from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import threading
import traceback
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

from . import (
    audit, canlii, citations, discovery, export, llm, maintenance, matters,
    pipeline, pleadings, updater,
)
from .prompts import (
    DEFAULT_TASK,
    available_tasks,
    get_template,
    missing_required_inputs,
    task_unavailable_reason,
)
from .vectorstore import file_hash, store_ok

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


# Phase 2b: style the verification markers the pipeline wrote into the answer.
#
# Applied AFTER bleach, deliberately. The alternative — adding <span class> to
# the sanitiser allowlist — would widen what MODEL output is permitted to emit,
# to solve a problem that is entirely about text WE inserted. Running afterwards
# keeps the allowlist exactly as strict as it was: the citation text inside each
# marker has already been escaped by bleach, and only our own literal marker
# shapes are matched.
_MARK_VERIFIED_HTML = re.compile(re.escape(citations.MARK_VERIFIED))
# One level of bracket nesting tolerated — a reporter-only citation is itself
# bracketed, so "[UNVERIFIED — ...: [2005] 2 S.C.R. 601]" must not be clipped at
# the inner "]". Same reason as citations.VERIFICATION_MARKER_PATTERN.
_MARK_FLAGGED_HTML = re.compile(
    r"\[(?:REMOVED|CITATION MISMATCH|UNVERIFIED)"
    r"(?:[^\[\]]|\[[^\[\]]*\])*\]",
    re.DOTALL,
)


def decorate_verification_markers(html: str) -> str:
    """Turn the plain-text verification markers into styled inline badges.

    The canonical markers in `answer` stay ASCII/WinAnsi-safe so the Word and
    PDF exports can render them; the check mark exists only here, in the one
    renderer where a U+2713 is guaranteed to have a glyph."""
    html = _MARK_VERIFIED_HTML.sub(
        '<span class="cite-ok">&#10003; verified in CanLII</span>', html
    )
    return _MARK_FLAGGED_HTML.sub(
        lambda m: f'<span class="cite-bad">{m.group(0)}</span>', html
    )


def _store_ok() -> tuple[bool, str | None]:
    """Is the local vector store openable?

    Phase 3: this used to ping Qdrant over HTTP. The embedded store has no
    daemon, so the only failure modes left are filesystem ones — a path that is
    not writable, a corrupt directory, or another process holding it.""" 
    return store_ok()


# --------------------------------------------------------------------------
# File-scope selection (Session 7)
#
# One shared shape for the selector, used by every matter-mode task. The three
# modes -- all / selected / single -- were previously two separate controls
# that could contradict each other, with the server silently preferring one.
# --------------------------------------------------------------------------
SCOPE_ALL = "all"
SCOPE_SELECTED = "selected"
SCOPE_SINGLE = "single"

# Tasks that reason about the matter as a whole. They keep subset selection --
# narrowing which documents inform the analysis is meaningful -- but single-file
# mode is a contradiction and stays unavailable.
WHOLE_MATTER_TASKS = ("compare_clauses", "suggest_cases")


def _matter_files_for_selection(files) -> list[dict]:
    """Shape a matter's files for the selector partial.

    Unqueryable files are INCLUDED but disabled, with the reason shown. Hiding
    them would make a lawyer wonder where a document went; showing them greyed
    out with "cannot be searched" turns an invisible gap into a visible, fixable
    one. Disabled inputs are not submitted, and every submitted id is still
    authorized server-side regardless.
    """
    out = []
    for f in files:
        queryable = matters.is_queryable(f.ingest_status)
        badge = matters.STATUS_LABELS.get(f.ingest_status, f.ingest_status)
        if f.ingest_status == "ingested":
            badge, badge_class, note = "", "", ""
        elif f.ingest_status == "ocr_low_quality":
            badge_class = "badge-warn"
            note = "answers from this file may be thin"
        else:
            badge_class = "badge-bad"
            note = "cannot be searched — needs re-processing"
        out.append({
            "id": f.id,
            "filename": f.filename,
            "file_type": f.file_type,
            "queryable": queryable,
            "status": f.ingest_status,
            "status_badge": badge,
            "badge_class": badge_class,
            "status_note": note,
        })
    return out


def _resolve_scope(conn, matter_id: int, task: str, form, queryable_files):
    """Turn the submitted scope controls into (files, single_file, error).

    Exactly one of `files` / `single_file` is returned. Every submitted id is
    checked for membership of THIS matter and for queryability, so a tampered
    or stale id is a clear refusal rather than a 500 or a silent drop.
    """
    mode = (form.get("scope_mode") or SCOPE_ALL).strip()

    if mode == SCOPE_SINGLE and task not in WHOLE_MATTER_TASKS:
        raw = (form.get("file_id") or "").strip()
        if not raw:
            return None, None, "Please choose a file, or select all files."
        try:
            fid = int(raw)
        except (TypeError, ValueError):
            return None, None, "Please choose a file to query."
        try:
            mf = matters.get_file_in_matter(conn, matter_id, fid)
        except matters.MatterError as e:
            return None, None, str(e)
        # is_queryable, not == "ingested": Session 6a made ocr_low_quality a
        # searchable status, and the whole-matter path already honours that.
        if not matters.is_queryable(mf.ingest_status):
            return None, None, (
                f"{mf.filename} cannot be searched "
                f"(status: {matters.STATUS_LABELS.get(mf.ingest_status, mf.ingest_status)}). "
                "Re-process it from the file list above."
            )
        return None, mf, None

    if mode == SCOPE_SELECTED:
        picked: set[int] = set()
        for raw in form.getlist("file_ids"):
            try:
                fid = int(raw)
            except (TypeError, ValueError):
                return None, None, "Invalid file selection."
            try:
                sel = matters.get_file_in_matter(conn, matter_id, fid)
            except matters.MatterError as e:
                return None, None, str(e)
            if not matters.is_queryable(sel.ingest_status):
                return None, None, (
                    f"{sel.filename} cannot be searched "
                    f"(status: {matters.STATUS_LABELS.get(sel.ingest_status, sel.ingest_status)})."
                )
            picked.add(fid)
        if picked:
            chosen = [f for f in queryable_files if f.id in picked]
            if not chosen:
                return None, None, "None of the selected files can be searched."
            return chosen, None, None
        # An empty selection means "all files" -- the same as not choosing.

    return list(queryable_files), None, None


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
    result, template, task, structured_inputs, back_url, back_label, matter_name=None,
    scope_mode="all", scope_names=None, scope_total=0,
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
        # Session 7: what the run was SCOPED to, which is a different fact from
        # "Drew on" / retrieved_sources. Scope is what the lawyer chose;
        # provenance is what actually grounded the answer. A file can be in
        # scope and contribute nothing, and the difference matters.
        scope_mode=scope_mode,
        scope_names=scope_names or [],
        scope_total=scope_total,
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
        retrieval_warnings=result.retrieval_warnings,
        email_metadata=result.email_metadata,
        attachment_warnings=result.attachment_warnings,
        answer_html=decorate_verification_markers(
            render_markdown(result.answer)
        ),
        authority_mode=result.authority_mode,
        verification=result.verification,
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
    # Liveness probe (Phase 3 Session 3).
    #
    # The packaged launcher polls this to decide when the server is actually
    # serving before it opens a browser, rather than sleeping a guessed number
    # of seconds. The "app" field is deliberately a fixed marker string: it
    # lets a caller tell "Matter Clerk is up on this port" apart from "some
    # unrelated process holds this port", which a 200 alone would not.
    # ----------------------------------------------------------------------
    @app.route("/healthz")
    def healthz():
        return {"app": "matter-clerk", "ok": True}

    @app.context_processor
    def inject_banners():
        """Notices and the update bar, for every template that extends base.

        The update bar is deliberately confined to the matters list. It must
        never appear over a result the lawyer is reading, and never while a
        task is being set up -- an offer to close the application mid-draft is
        an offer to lose work.
        """
        upd = updater.available_update() if request.endpoint == "index" else None
        try:
            on_page = request.endpoint in ("index", "matter_detail")
            notices = maintenance.take_notices() if on_page else []
        except Exception:
            notices = []
        return {"update": upd, "notices": notices}

    # ----------------------------------------------------------------------
    # Global error handler (Session 6a)
    #
    # A lawyer must never meet Flask's bare "Internal Server Error". Before
    # this, an unhandled exception in a task showed exactly that, with the
    # only useful text stranded in a console window behind the browser -- which
    # is how the field report arrived: a screenshot of a traceback.
    # ----------------------------------------------------------------------
    @app.errorhandler(Exception)
    def handle_unexpected(e):
        from werkzeug.exceptions import HTTPException

        if isinstance(e, HTTPException):
            return e                      # 404s and friends keep their meaning

        tb = traceback.format_exc()
        log.error("unhandled error on %s: %s", request.path, tb)
        try:
            audit.log_event(
                "unhandled_error",
                path=request.path,
                method=request.method,
                matter_id=request.view_args.get("matter_id") if request.view_args else None,
                task=request.form.get("task") if request.form else None,
                error=f"{type(e).__name__}: {e}",
                traceback=tb,
            )
        except Exception:                                         # noqa: BLE001
            pass
        return render_template("error.html", error_type=type(e).__name__), 500

    # ----------------------------------------------------------------------
    # Recovery and support routes (Session 6a)
    # ----------------------------------------------------------------------
    @app.post("/matters/<int:matter_id>/files/<int:file_id>/reingest")
    def reingest_file(matter_id: int, file_id: int):
        conn = matters.connect()
        try:
            mf = matters.get_file(conn, file_id)
            if mf is None or mf.matter_id != matter_id:
                abort(404)
            stored = Path(mf.stored_path)
            if not stored.is_file():
                flash(f"{mf.filename}: the stored copy is missing. Upload the "
                      f"file again.", "error")
                return redirect(url_for("matter_detail", matter_id=matter_id))
            try:
                # reindex=True: the whole point is to rebuild, so never take
                # the cache path -- a damaged collection is what we are replacing.
                outcome = pipeline.ingest_file(
                    stored, mf.filename, collection=mf.collection,
                    reindex=True, matter_id=matter_id,
                )
                if outcome.quality == "ocr_low_quality":
                    matters.mark_file_ingested(
                        conn, file_id, status="ocr_low_quality",
                        note=outcome.quality_detail,
                    )
                    flash(f"Re-processed {mf.filename}, but the scan quality is "
                          f"still poor. {outcome.quality_detail}", "warn")
                else:
                    matters.mark_file_ingested(conn, file_id)
                    flash(f"Re-processed {mf.filename}.", "ok")
            except pipeline.PdfHasNoText as e:
                matters.mark_file_no_text(conn, file_id, str(e))
                flash(f"{mf.filename}: still no readable text. A clearer scan "
                      f"is needed.", "warn")
            except Exception as e:                                # noqa: BLE001
                matters.mark_file_failed(conn, file_id, str(e))
                flash(f"{mf.filename}: re-processing failed: {e}", "error")
        finally:
            conn.close()
        return redirect(url_for("matter_detail", matter_id=matter_id))

    @app.post("/matters/<int:matter_id>/sort")
    def set_matter_sort(matter_id: int):
        """Remember this matter's file ordering. A preference, not matter data:
        stored in the data directory's ui_prefs.json rather than the database,
        so no schema change and no migration on installed copies."""
        order = (request.form.get("sort") or matters.DEFAULT_SORT).strip()
        if order in matters.SORT_ORDERS:
            maintenance.set_matter_sort(matter_id, order)
        return redirect(url_for("matter_detail", matter_id=matter_id))

    @app.post("/matters/<int:matter_id>/remove-failed")
    def remove_failed_files(matter_id: int):
        conn = matters.connect()
        removed = 0
        try:
            for mf in matters.list_files(conn, matter_id):
                if matters.is_queryable(mf.ingest_status):
                    continue
                # The stored document is left on disk deliberately: it is the
                # lawyer's own file, and deleting client material to tidy a
                # list is not a trade this application gets to make.
                matters.delete_file(conn, mf.id)
                removed += 1
        finally:
            conn.close()
        flash(f"Removed {removed} unreadable file(s) from this matter. Your "
              f"original documents were not deleted.", "ok" if removed else "warn")
        return redirect(url_for("matter_detail", matter_id=matter_id))

    @app.post("/diagnostics")
    def run_diagnostics():
        try:
            path = maintenance.write_diagnostic_report()
            flash(f"Diagnostic report saved to {path}. It contains no document "
                  f"text, matter names or file names -- only structure. Email "
                  f"it to your Matter Clerk contact.", "ok")
        except Exception as e:                                    # noqa: BLE001
            flash(f"Could not create the diagnostic report: {e}", "error")
        return redirect(request.referrer or url_for("index"))

    @app.post("/update/dismiss")
    def dismiss_update():
        updater.dismiss()
        return redirect(request.referrer or url_for("index"))

    @app.post("/update/install")
    def install_update():
        info = updater.available_update()
        if not info:
            flash("No update is pending.", "warn")
            return redirect(url_for("index"))
        try:
            path = updater.download_installer(info)
        except Exception as e:                                    # noqa: BLE001
            flash(f"Could not download the update: {e}", "error")
            return redirect(url_for("index"))
        updater.launch_installer(path)
        # The installer replaces files this process holds open, so it must go.
        threading.Timer(1.5, lambda: os._exit(0)).start()
        return render_template("updating.html", version=info["version"])

    # ----------------------------------------------------------------------
    # Render helpers (kept inside create_app so url_for is available)
    # ----------------------------------------------------------------------
    def render_ad_hoc(status=200, **kw):
        ok, err = _store_ok()
        defaults = dict(
            store_ok=ok, store_err=err, error=None,
            # Matter-only tasks (Compare Clauses) never appear on the ad-hoc form.
            tasks=available_tasks(None), selected_task=DEFAULT_TASK,
            values={}, top_k="", reindex=False, limitation_signals=None,
            action=url_for("ad_hoc_query"),
        )
        defaults.update(kw)
        return render_template("ad_hoc.html", **defaults), status

    def render_matter_detail(conn, matter_id, status=200, **kw):
        matter = matters.get_matter(conn, matter_id)
        # Session 7: one ordering for the file list AND the scope selector, so
        # a lawyer picking "the third one down" sees the same third one in both.
        sort_order = maintenance.get_matter_sort(matter_id)
        files = matters.sort_files(matters.list_files(conn, matter_id), sort_order)
        queryable = [f for f in files if matters.is_queryable(f.ingest_status)]
        ok, err = _store_ok()
        defaults = dict(
            store_ok=ok, store_err=err, error=None,
            matter=matter, files=files,
            queryable_files=queryable, matter_files=queryable,
            # The selector shows unqueryable files too, disabled with a reason:
            # a document that silently vanishes from the list is a worse
            # experience than one shown greyed out with a way to fix it.
            selector_files=_matter_files_for_selection(files),
            sort_order=sort_order,
            sort_orders=matters.SORT_ORDERS,
            sort_labels=matters.SORT_LABELS,
            # Compare Clauses appears only once the matter holds 2+ ingested files.
            tasks=available_tasks(len(queryable)),
            compare_max_files=pipeline.COMPARE_MAX_FILES,
            selected_task=DEFAULT_TASK,
            values={}, top_k="", limitation_signals=None, limitation_by_file=None,
            selected_file_id=None, selected_file_ids=[], scope_mode="all",
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
                outcome = pipeline.ingest_file(stored, filename, collection=coll,
                                               matter_id=matter.id)
                if outcome.quality == "ocr_low_quality":
                    # Still indexed and still queryable -- the lawyer decides
                    # whether a better scan is worth chasing. Silently accepting
                    # it is what produced "2 events from 28 files".
                    matters.mark_file_ingested(
                        conn, row.id, status="ocr_low_quality",
                        note=outcome.quality_detail,
                    )
                    flash(f"Added {filename}, but the scan quality is poor. "
                          f"{outcome.quality_detail}", "warn")
                else:
                    matters.mark_file_ingested(conn, row.id)
                    flash(f"Added {filename}.", "ok")
            except pipeline.PdfHasNoText as e:
                matters.mark_file_no_text(conn, row.id, str(e))
                flash(f"{filename}: no readable text found. Re-upload a "
                      f"clearer scan or a text-based PDF.", "warn")
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
        ok, err = _store_ok()
        return render_template(
            "matters.html", matters=ms, store_ok=ok, store_err=err, error=None
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
                scope_mode=(request.form.get("scope_mode") or "all"),
                selected_file_ids=request.form.getlist("file_ids"),
            )

            # Server-side counterparts to the form's task filtering + JS gating
            # (Day 4c). The form hides Compare Clauses on a <2-file matter and
            # hides the single-file picker when it is selected; these catch a
            # POST that arrives regardless.
            n_ingested = len([
                f for f in matters.list_files(conn, matter_id)
                if matters.is_queryable(f.ingest_status)
            ])
            unavailable = task_unavailable_reason(task, n_ingested)
            if unavailable:
                return render_matter_detail(
                    conn, matter_id, status=400, error=unavailable, **common
                )
            # Case discovery searches CanLII with concepts drawn from the whole
            # matter. Restricting it to one file is a contradiction the form
            # already hides; this catches a POST that arrives anyway.
            if task == discovery.TASK_ID and file_id_raw:
                return render_matter_detail(
                    conn, matter_id, status=400,
                    error="Suggest Relevant Cases draws on the whole matter. "
                          "Clear the single-file restriction and run it again.",
                    **common
                )
            if task == pipeline.COMPARE_TASK_ID and file_id_raw:
                return render_matter_detail(
                    conn, matter_id, status=400,
                    error="Compare Clauses runs across the documents in this "
                          "matter. Clear the single-file restriction, and use "
                          "“Compare across which files?” to narrow the "
                          "comparison instead.", **common
                )

            # Session 7: one scope resolution for every task, replacing the
            # old two-control dispatch. `_resolve_scope` returns either a file
            # LIST (all / selected) or a single file, having authorized every
            # submitted id against this matter first.
            queryable_files = [
                f for f in matters.list_files(conn, matter_id)
                if matters.is_queryable(f.ingest_status)
            ]
            queryable_files = matters.sort_files(
                queryable_files, maintenance.get_matter_sort(matter_id)
            )
            if not queryable_files:
                return render_matter_detail(
                    conn, matter_id, status=400,
                    error="This matter has no successfully ingested files to "
                          "query.", **common
                )

            files, mf, scope_error = _resolve_scope(
                conn, matter_id, task, request.form, queryable_files
            )
            if scope_error:
                return render_matter_detail(
                    conn, matter_id, status=400, error=scope_error, **common
                )

            # What the run was actually scoped to. Reported on the result page
            # and, when it is not the default, in the audit log -- "which files
            # did this run touch" is the question a later review asks.
            scope_total = len(queryable_files)
            if mf is not None:
                scope_mode_used, scope_names, scope_ids = "single", [mf.filename], [mf.id]
            elif files is not None and len(files) < scope_total:
                scope_mode_used = "selected"
                scope_names = [f.filename for f in files]
                scope_ids = [f.id for f in files]
            else:
                scope_mode_used = "all"
                scope_names = [f.filename for f in (files or [])]
                scope_ids = []

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
                elif task == discovery.TASK_ID:
                    # Structurally distinct from every other task: it returns a
                    # CaseDiscoveryResult (a shortlist of external cases), not a
                    # PipelineResult (a grounded answer over matter documents),
                    # so it renders its own page and returns early rather than
                    # falling through to _render_result.
                    disc = discovery.run_case_discovery(
                        files=files,
                        matter_id=matter_id,
                        matter_name=matters.get_matter(conn, matter_id).name,
                        structured_inputs=structured_inputs,
                    )
                    matters.touch_last_queried(conn, matter_id)
                    return render_template(
                        "case_discovery.html",
                        result=disc,
                        back_url=url_for("matter_detail", matter_id=matter_id),
                        back_label="Back to matter",
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
            # A missing API key is a configuration problem, not a server fault:
            # 503 with the remedy in the message, never an opaque 500.
            except llm.MissingAPIKey as e:
                return render_matter_detail(
                    conn, matter_id, status=503, error=str(e), **common
                )
            # CanLII failures are refusals with a specific cause, never opaque
            # 500s: the lawyer needs to know whether to fix a key, wait out a
            # rate limit, or rephrase the question.
            except canlii.CanLIIAuthError as e:
                return render_matter_detail(
                    conn, matter_id, status=503, error=str(e), **common
                )
            except canlii.CanLIIBudgetExceeded as e:
                return render_matter_detail(
                    conn, matter_id, status=429, error=str(e), **common
                )
            except canlii.CanLIIThrottled as e:
                return render_matter_detail(
                    conn, matter_id, status=429,
                    error=f"{e} Wait a moment and run the task again.", **common
                )
            except canlii.CanLIIUnavailable as e:
                return render_matter_detail(
                    conn, matter_id, status=503, error=str(e), **common
                )
            except discovery.CaseDiscoveryError as e:
                return render_matter_detail(
                    conn, matter_id, status=422, error=str(e), **common
                )
            except pipeline.VectorStoreUnreachable as e:
                return render_matter_detail(
                    conn, matter_id, status=503,
                    store_ok=False, store_err=str(e), **common
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
                    # Only when the lawyer narrowed the scope: on the default
                    # path this stays absent, so existing audit records keep
                    # exactly the shape they had before Session 7.
                    **({"scope": scope_mode_used, "scoped_file_ids": scope_ids}
                       if scope_mode_used != "all" else {}),
                )

            matters.touch_last_queried(conn, matter_id)
            return _render_result(
                result, template, task, structured_inputs,
                back_url=url_for("matter_detail", matter_id=matter_id),
                back_label="Back to matter",
                matter_name=matters.get_matter(conn, matter_id).name,
                scope_mode=scope_mode_used,
                scope_names=scope_names,
                scope_total=scope_total,
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
            except llm.MissingAPIKey as e:
                return render_ad_hoc(status=503, error=str(e), **common)
            except pipeline.VectorStoreUnreachable as e:
                return render_ad_hoc(status=503, store_ok=False, store_err=str(e), **common)
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
    # Phase 3: Qdrant's httpx chatter is gone with Qdrant, but chromadb and the
    # HF hub still log at INFO. Quiet them so successful calls are silent and
    # failures still surface.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("chromadb").setLevel(logging.WARNING)
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
