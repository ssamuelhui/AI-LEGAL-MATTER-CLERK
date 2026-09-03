from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import time
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
    audit, canlii, citations, costs, discovery, exhaustive, export,
    first_run_wizard, ingest_docx, ingest_xlsx, llm, maintenance, matters,
    model_registry, pipeline, pleadings, runs, updater,
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
# Session 9: Word and Excel join PDF and email. One tuple, so the upload
# validation, the accept attribute and the CLI cannot drift apart.
SUPPORTED_SUFFIXES = (".pdf", ".eml", ".docx", ".xlsx")
UPLOAD_ACCEPT = (
    "application/pdf,.pdf,message/rfc822,.eml,"
    ".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
    ".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)

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
        elif f.ingest_status == "password_protected":
            badge_class = "badge-bad"
            note = "remove the password and re-upload"
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


def build_system_prompt_for(template, form):
    """The system prompt this submission would produce -- used by the estimator
    so the token count reflects the real prompt, not an approximation of it."""
    from .prompts import build_system_prompt

    return build_system_prompt(
        template, _collect_web_inputs(template, form), cross_document=True
    )


def _task_labels() -> dict:
    """Task id -> label, so the cost log reads in the lawyer's vocabulary."""
    try:
        return {t.id: t.label for t in available_tasks(None)}
    except Exception:                                             # noqa: BLE001
        return {}


def _task_picker():
    """Sorted model list for the task form. Never raises and never blocks:
    a failed fetch degrades to the three recommended models."""
    try:
        return model_registry.sort_for_picker(
            list(model_registry.available_models()["models"])
        )
    except Exception:                                             # noqa: BLE001
        return model_registry.sort_for_picker(model_registry._fallback_models())


def _task_model(task: str) -> str:
    model_id, _warning = model_registry.resolve_model(task)
    return model_id


def _finish_cost(acc, matter_name, started, *, status,
                 detail: str = "", was_exhaustive: bool = False,
                 run_id: str | None = None) -> dict | None:
    """Write the cost row for a finished run and return it for the page.

    Called from the success path and from the failure path alike, because the
    tokens a failed run spent were still spent -- and a lawyer looking for
    "why did that disappear" needs to find the attempt rather than silence.
    """
    import time as _time

    if acc is None or getattr(acc, "_recorded", False):
        return None
    # Marked before the write, not after: the success path records here and the
    # surrounding `finally` records only if this never ran. Without the flag a
    # completed run would be billed twice.
    acc._recorded = True
    duration = round(_time.monotonic() - started, 2)
    row_id = costs.record_from_accumulator(
        acc, matter_name=matter_name, duration_seconds=duration,
        was_exhaustive=was_exhaustive, status=status, detail=detail,
        run_id=run_id,
    )
    return {
        "id": row_id,
        "cost_usd": None if acc.cost_unavailable else round(acc.cost_usd, 6),
        "input_tokens": acc.input_tokens,
        "output_tokens": acc.output_tokens,
        "calls": acc.calls,
        "model": (acc.models_used[0] if acc.models_used else acc.model),
        "duration_seconds": duration,
        "status": status,
        "detail": detail,
        "was_exhaustive": was_exhaustive,
    }


def _mask_key(value: str | None) -> str:
    """Show enough to recognise a key, never enough to use or size it.

    A fixed number of dots rather than one per hidden character: the length of
    an API key is itself information, and this string is rendered on a page
    that may be screenshotted into a support thread.
    """
    if not value:
        return ""
    value = value.strip()
    if len(value) <= 12:
        return "\u2022" * 8
    return value[:8] + "\u2026" + "\u2022" * 8 + "\u2026" + value[-4:]


def _update_env_var(key: str, value: str) -> None:
    """Rewrite one variable in the data directory .env, then in this process.

    os.environ is set explicitly because python-dotenv does not override
    variables that are already set, so rewriting the file alone would leave the
    old key live for the rest of the session.
    """
    path = first_run_wizard.env_path()
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    out, replaced = [], False
    for line in lines:
        if line.strip().startswith(key + "="):
            out.append(key + "=" + value)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(key + "=" + value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    first_run_wizard._restrict_to_current_user(path)
    os.environ[key] = value


def prompts_exhaustive_tasks():
    from .prompts import EXHAUSTIVE_TASKS

    return EXHAUSTIVE_TASKS


def _start_exhaustive(state, scope_files, task, structured_inputs, matter_id):
    """Launch the background run and persist its result when it finishes.

    The PipelineResult is serialised to disk rather than held in memory: the
    whole point of the run registry is that closing the browser, or restarting
    Flask, does not lose a run the lawyer has already paid for.
    """
    def work(st):
        import time as _time

        started = _time.monotonic()
        # Opened inside the worker: the accumulator is thread-local and this
        # runs on a background thread, not on the request that launched it.
        acc = llm.start_cost_run(task=task, matter_id=matter_id,
                                 model=st.model)
        matter_name = None
        try:
            c = matters.connect()
            try:
                m = matters.get_matter_any(c, matter_id)
                matter_name = m.name if m else None
            finally:
                c.close()
        except Exception:                                         # noqa: BLE001
            pass

        def progress(batch, batches, names, run):
            runs.update(
                st, batch=batch, batches=batches, current_files=list(names),
                prompt_tokens=run.prompt_tokens,
                completion_tokens=run.completion_tokens,
                cost_usd=round(run.cost_usd, 4), seconds=round(run.seconds, 1),
            )

        try:
            result = pipeline.run_exhaustive_matter_query(
                files=scope_files, task=task,
                structured_inputs=structured_inputs,
                matter_id=matter_id, progress=progress,
                should_cancel=lambda: runs.cancel_requested(st.run_id),
            )
        except Exception:
            # Tokens spent before the failure were still spent.
            _finish_cost(acc, matter_name, started,
                         status=costs.STATUS_FAILED,
                         detail=f"failed at batch {st.batch} of {st.batches}",
                         was_exhaustive=True, run_id=st.run_id)
            llm.end_cost_run()
            raise
        runs.save_result(st.run_id, result.model_dump_json())

        cancelled = runs.cancel_requested(st.run_id)
        runs.update(
            st,
            status=runs.CANCELLED if cancelled else runs.DONE,
            batch=st.batches or st.batch,
            collapsed_duplicates=getattr(result, "collapsed_duplicates", 0) or 0,
        )
        _finish_cost(
            acc, matter_name, started,
            status=costs.STATUS_CANCELLED if cancelled else costs.STATUS_COMPLETED,
            detail=(f"cancelled at batch {st.batch} of {st.batches}"
                    if cancelled else ""),
            was_exhaustive=True, run_id=st.run_id,
        )
        llm.end_cost_run()

        audit.log_event(
            "matter_query",
            matter_id=matter_id, task=task, mode=st.mode,
            exhaustive=True, model=st.model, run_id=st.run_id,
            model_requested=st.model_requested or st.model,
            model_used=st.model,
            # A separate boolean rather than something a later auditor has to
            # reconstruct by comparing two strings.
            model_coerced=bool(st.model_requested and st.model_requested != st.model),
            batches=st.batches, chunks_processed=result.top_k,
            prompt_tokens=st.prompt_tokens, completion_tokens=st.completion_tokens,
            cost_usd=st.cost_usd, seconds=st.seconds,
            retrieved_file_ids=result.retrieved_file_ids,
            cancelled=cancelled,
        )

    runs.start(state, work)


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
    scope_mode="all", scope_names=None, scope_total=0, run_state=None,
    cost=None,
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
        # Session 8: exhaustive provenance. A lawyer relying on this output must
        # be able to see WHICH model produced it and what it cost, especially
        # since exhaustive runs override the configured model.
        run_state=run_state,
        # Session 11: what this run cost, for transcription into billing.
        cost=cost,
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
            except (ingest_docx.DocxPasswordProtected,
                    ingest_xlsx.XlsxPasswordProtected) as e:
                matters.mark_file_password_protected(conn, file_id, str(e))
                flash(str(e), "warn")
            except pipeline.PdfHasNoText as e:
                matters.mark_file_no_text(conn, file_id, str(e))
                flash(f"{mf.filename}: {e}", "warn")
            except Exception as e:                                # noqa: BLE001
                matters.mark_file_failed(conn, file_id, str(e))
                flash(f"{mf.filename}: re-processing failed: {e}", "error")
        finally:
            conn.close()
        return redirect(url_for("matter_detail", matter_id=matter_id))

    # ----------------------------------------------------------------------
    # Exhaustive run pages (Session 8)
    # ----------------------------------------------------------------------
    @app.get("/runs/<run_id>")
    def run_page(run_id: str):
        state = runs.load(run_id)
        if state is None:
            abort(404)
        if state.status == runs.DONE or (
            state.status == runs.CANCELLED and runs.load_result(run_id)
        ):
            payload = runs.load_result(run_id)
            if payload:
                result = pipeline.PipelineResult(**payload)
                template = get_template(state.task)
                conn = matters.connect()
                try:
                    name = matters.get_matter(conn, state.matter_id).name
                finally:
                    conn.close()
                return _render_result(
                    result, template, state.task, {},
                    back_url=url_for("matter_detail", matter_id=state.matter_id),
                    back_label="Back to matter", matter_name=name,
                    scope_mode="selected" if state.scope_names else "all",
                    scope_names=state.scope_names,
                    scope_total=len(state.scope_names),
                    run_state=state,
                )
        return render_template("run.html", run=state)

    @app.get("/runs/<run_id>/status")
    def run_status(run_id: str):
        """Polled every 1.5 s by the run page. Deliberately tiny."""
        state = runs.load(run_id)
        if state is None:
            return {"status": "missing"}, 404
        return {
            "status": state.status,
            "batch": state.batch,
            "batches": state.batches,
            "files_total": state.files_total,
            "current_files": state.current_files,
            "cost_usd": state.cost_usd,
            "prompt_tokens": state.prompt_tokens,
            "completion_tokens": state.completion_tokens,
            "seconds": state.seconds,
            "error": state.error,
            "cancel_requested": state.cancel_requested,
            "done": state.status in runs.TERMINAL,
        }

    @app.post("/runs/<run_id>/cancel")
    def run_cancel(run_id: str):
        if runs.request_cancel(run_id):
            flash("Cancelling after the current batch finishes. Any completed "
                  "batches will still be shown.", "info")
        return redirect(url_for("run_page", run_id=run_id))

    @app.post("/matters/<int:matter_id>/estimate")
    def estimate_exhaustive(matter_id: int):
        """Figures for the pre-run confirmation dialog, for the current selection."""
        conn = matters.connect()
        try:
            queryable = [
                f for f in matters.list_files(conn, matter_id)
                if matters.is_queryable(f.ingest_status)
            ]
            queryable = matters.sort_files(
                queryable, maintenance.get_matter_sort(matter_id)
            )
            task = request.form.get("task") or DEFAULT_TASK
            files, mf, err = _resolve_scope(
                conn, matter_id, task, request.form, queryable
            )
            if err:
                return {"error": err}, 400
            scope = [mf] if mf is not None else (files or [])
        finally:
            conn.close()

        from .vectorstore import connect as vs_connect

        texts, unreadable = exhaustive.gather_all_chunks(vs_connect(), scope)
        template = get_template(task)
        system_prompt = build_system_prompt_for(template, request.form)
        est = exhaustive.estimate_run(texts, system_prompt)
        est["unreadable"] = unreadable
        est["cost_low"] = round(est["cost_low"], 2)
        est["cost_high"] = round(est["cost_high"], 2)
        return est

    # ----------------------------------------------------------------------
    # Soft delete, restore and the Deleted items view (Session 10)
    # ----------------------------------------------------------------------
    @app.post("/matters/<int:matter_id>/files/<int:file_id>/delete")
    def delete_file(matter_id: int, file_id: int):
        conn = matters.connect()
        try:
            mf = matters.get_file(conn, file_id)
            if mf is None or mf.matter_id != matter_id:
                abort(404)
            matters.soft_delete_file(conn, file_id)
            audit.log_event("file_deleted", matter_id=matter_id, file_id=file_id,
                            filename=mf.filename, soft=True,
                            recovery_days=matters.RECOVERY_WINDOW_DAYS)
            flash("Deleted " + mf.filename + ". You can restore it for "
                  + str(matters.RECOVERY_WINDOW_DAYS) + " days.", "ok")
        finally:
            conn.close()
        return redirect(url_for("matter_detail", matter_id=matter_id))

    @app.post("/deleted/files/<int:file_id>/restore")
    def restore_file(file_id: int):
        conn = matters.connect()
        try:
            mf = matters.get_file_any(conn, file_id)
            if mf is None:
                abort(404)
            matters.restore_file(conn, file_id)
            audit.log_event("file_restored", matter_id=mf.matter_id,
                            file_id=file_id, filename=mf.filename)
            flash("Restored " + mf.filename + ".", "ok")
        finally:
            conn.close()
        return redirect(url_for("deleted_items"))

    @app.post("/matters/<int:matter_id>/delete")
    def delete_matter(matter_id: int):
        conn = matters.connect()
        try:
            matter = matters.get_matter_any(conn, matter_id)
            if matter is None:
                abort(404)
            # Re-checked server side. A client-only gate on an irreversible
            # action is not a gate.
            typed = (request.form.get("confirm_name") or "").strip()
            if typed != matter.name.strip():
                flash("The name you typed did not match, so nothing was "
                      "deleted.", "error")
                return redirect(url_for("matter_detail", matter_id=matter_id))
            matters.soft_delete_matter(conn, matter_id)
            audit.log_event("matter_deleted", matter_id=matter_id,
                            name=matter.name, soft=True,
                            recovery_days=matters.RECOVERY_WINDOW_DAYS)
            flash("Deleted the matter " + matter.name + ". You can restore it "
                  "for " + str(matters.RECOVERY_WINDOW_DAYS) + " days.", "ok")
        finally:
            conn.close()
        return redirect(url_for("index"))

    @app.post("/deleted/matters/<int:matter_id>/restore")
    def restore_matter(matter_id: int):
        conn = matters.connect()
        try:
            matter = matters.get_matter_any(conn, matter_id)
            if matter is None:
                abort(404)
            matters.restore_matter(conn, matter_id)
            audit.log_event("matter_restored", matter_id=matter_id,
                            name=matter.name)
            flash("Restored " + matter.name + ".", "ok")
        finally:
            conn.close()
        return redirect(url_for("deleted_items"))

    # ----------------------------------------------------------------------
    # Task cost log (Session 11)
    # ----------------------------------------------------------------------
    def _cost_view(args):
        """Shared query for the log page and the CSV export, so what a lawyer
        exports is exactly what they were looking at."""
        matter_id = args.get("matter")
        matter_id = int(matter_id) if (matter_id or "").isdigit() else None
        period = args.get("period") or "30"
        sort = args.get("sort") or "timestamp"
        direction = args.get("dir") or "desc"
        date_from = (args.get("from") or "").strip() or None
        date_to = (args.get("to") or "").strip() or None
        conn = costs.connect()
        try:
            rows = costs.query(conn, matter_id=matter_id, period=period,
                               sort=sort, direction=direction,
                               date_from=date_from, date_to=date_to)
            options = costs.matter_options(conn)
        finally:
            conn.close()
        return rows, options, {
            "matter": matter_id, "period": period, "sort": sort,
            "dir": direction, "from": date_from or "", "to": date_to or "",
        }

    @app.get("/costs")
    def cost_log():
        rows, options, state = _cost_view(request.args)
        total, count, unknown = costs.totals(rows)
        return render_template(
            "costs.html", rows=rows, matter_options=options, state=state,
            total=total, count=count, unknown=unknown,
            periods=costs.PERIODS, task_labels=_task_labels(),
        )

    @app.get("/costs.csv")
    def cost_csv():
        rows, _options, _state = _cost_view(request.args)
        # utf-8-sig so Excel opens accented matter names without an import
        # dialog -- an accountant should be able to double-click the file.
        body = costs.to_csv(rows).encode("utf-8-sig")
        resp = make_response(body)
        resp.headers["Content-Type"] = "text/csv; charset=utf-8"
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="{costs.csv_filename()}"'
        )
        return resp

    @app.get("/deleted")
    def deleted_items():
        conn = matters.connect()
        try:
            deleted_matters, deleted_files = matters.list_deleted(conn)
        finally:
            conn.close()
        return render_template(
            "deleted.html",
            deleted_matters=deleted_matters, deleted_files=deleted_files,
            days_remaining=matters.days_remaining,
            window=matters.RECOVERY_WINDOW_DAYS,
        )

    # ----------------------------------------------------------------------
    # Settings (Session 10) -- API keys and per-task models
    # ----------------------------------------------------------------------
    @app.get("/settings")
    def settings():
        catalogue = model_registry.available_models()
        picker = model_registry.sort_for_picker(list(catalogue["models"]))
        return render_template(
            "settings.html",
            openrouter_masked=_mask_key(os.environ.get("OPENROUTER_API_KEY")),
            canlii_masked=_mask_key(os.environ.get("CANLII_API_KEY")),
            env_path=first_run_wizard.env_path(),
            catalogue=catalogue,
            picker=picker,
            preferences=model_registry.load_preferences(),
            tasks=available_tasks(None),
            default_model=model_registry.DEFAULT_MODEL,
            exhaustive_model=exhaustive.EXHAUSTIVE_MODEL,
        )

    @app.post("/settings/keys")
    def save_api_key():
        which = (request.form.get("which") or "").strip()
        value = (request.form.get("value") or "").strip()
        if which not in ("openrouter", "canlii"):
            abort(400)
        if not value:
            flash("No key was entered, so nothing was changed.", "warn")
            return redirect(url_for("settings"))

        # Tested before saving, against the same endpoints the first-run
        # wizard uses.
        if which == "openrouter":
            ok, message = first_run_wizard.test_openrouter(value)
            env_key = "OPENROUTER_API_KEY"
        else:
            ok, message = first_run_wizard.test_canlii(value)
            env_key = "CANLII_API_KEY"
        if not ok:
            # `message` reports status codes only and never echoes the key.
            flash("That key was not accepted: " + message, "error")
            return redirect(url_for("settings"))

        try:
            _update_env_var(env_key, value)
        except Exception as e:                                    # noqa: BLE001
            flash("Could not save the key: " + type(e).__name__, "error")
            return redirect(url_for("settings"))

        # No key material, not even a length: a length narrows the search
        # space and this log is retained indefinitely.
        audit.log_event("api_key_changed", which=which, tested_ok=True)
        flash("Key saved and verified. Tasks already running continue on the "
              "previous key; new tasks use this one.", "ok")
        return redirect(url_for("settings"))

    @app.post("/settings/model")
    def save_model_preference():
        task = (request.form.get("task") or "").strip()
        model_id = (request.form.get("model") or "").strip()
        if task not in {t.id for t in available_tasks(None)}:
            abort(400)
        if model_id:
            model_registry.save_preference(task, model_id)
            flash("Model for " + task + " set to " + model_id + ".", "ok")
        return redirect(request.referrer or url_for("settings"))

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
            action=url_for("ad_hoc_query"), estimate_url="",
            upload_accept=UPLOAD_ACCEPT,
            picker=_task_picker(), task_model=_task_model(DEFAULT_TASK),
            exhaustive_model=exhaustive.EXHAUSTIVE_MODEL,
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
            estimate_url=url_for("estimate_exhaustive", matter_id=matter_id),
            upload_accept=UPLOAD_ACCEPT,
            picker=_task_picker(), task_model=_task_model(DEFAULT_TASK),
            exhaustive_model=exhaustive.EXHAUSTIVE_MODEL,
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
        if suffix not in SUPPORTED_SUFFIXES:
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
                    suffix.lstrip("."),
                    sha, coll, str(stored),
                )
            except matters.DuplicateFileInMatter as e:
                # Session 10: the blocker may be a soft-deleted row. The
                # lawyer's intent is unambiguous -- they want this file in the
                # matter -- and refusing as a "duplicate" for a file they
                # cannot see would be baffling.
                prior = matters.find_deleted_file_by_hash(conn, matter.id, sha)
                if prior is not None:
                    matters.restore_file(conn, prior.id)
                    audit.log_event("file_restored", matter_id=matter.id,
                                    file_id=prior.id, filename=prior.filename,
                                    via="re-upload")
                    flash("This file was previously deleted and has been "
                          "restored to the matter.", "ok")
                else:
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
            except (ingest_docx.DocxPasswordProtected,
                    ingest_xlsx.XlsxPasswordProtected) as e:
                matters.mark_file_password_protected(conn, row.id, str(e))
                flash(str(e), "warn")
            except (ingest_docx.DocxUnreadable, ingest_xlsx.XlsxUnreadable) as e:
                matters.mark_file_failed(conn, row.id, str(e))
                flash(str(e), "error")
            except pipeline.PdfHasNoText as e:
                matters.mark_file_no_text(conn, row.id, str(e))
                flash(f"{filename}: {e}", "warn")
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
                gone = matters.deleted_matter_named(conn, name)
                if gone is not None:
                    flash("A deleted matter is using this name. Restore it "
                          "from Deleted items, or choose a different name.",
                          "warn")
                    return redirect(url_for("deleted_items"))
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

            # Session 10: the model this task is set to. Applied for the run
            # and recorded, so the result page and the audit log can both say
            # what was asked for as well as what was used.
            model_requested, model_warning = model_registry.resolve_model(task)
            if model_warning:
                flash(model_warning, "warn")
            pipeline.set_model_override(model_requested)

            # Session 8: exhaustive runs take minutes (measured 169.5 s over a
            # 9-file matter), so they execute as a background task and the
            # browser is redirected to a run page. Everything below this is the
            # unchanged synchronous path for every other mode.
            from .prompts import is_exhaustive

            if is_exhaustive(structured_inputs) and task in prompts_exhaustive_tasks():
                scope_files = [mf] if mf is not None else (files or [])
                existing = runs.active_run_for(matter_id)
                if existing:
                    # A second click, or another tab. Show the run in progress
                    # rather than refusing or starting a duplicate.
                    flash("An exhaustive analysis is already running for this "
                          "matter.", "info")
                    return redirect(url_for("run_page", run_id=existing))

                state = runs.create(
                    matter_id=matter_id, task=task,
                    model_requested=model_requested,
                    mode=(structured_inputs.get("detail_level")
                          or structured_inputs.get("mode") or "Exhaustive"),
                    model=exhaustive.EXHAUSTIVE_MODEL,
                    scope_names=[f.filename for f in scope_files],
                )
                clash = runs.acquire(matter_id, state.run_id)
                if clash:
                    return redirect(url_for("run_page", run_id=clash))

                _start_exhaustive(state, scope_files, task, structured_inputs,
                                  matter_id)
                return redirect(url_for("run_page", run_id=state.run_id))

            cost_started = time.monotonic()
            cost_status = costs.STATUS_FAILED
            cost_detail = ""
            matter_name_for_cost = None
            try:
                matter_name_for_cost = matters.get_matter_any(
                    conn, matter_id).name
            except Exception:                                     # noqa: BLE001
                pass

            cost_acc = llm.start_cost_run(
                task=task, matter_id=matter_id, model=model_requested)
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
            finally:
                # A row exists whatever happened. A run that dies before
                # reaching the model records $0.00 rather than nothing, so a
                # lawyer looking for a vanished task finds the attempt.
                if cost_acc is not None and not getattr(cost_acc, "_recorded", False):
                    _finish_cost(cost_acc, matter_name_for_cost, cost_started,
                                 status=cost_status, detail=cost_detail)
                llm.end_cost_run()
            if mf is None:
                audit.log_event(
                    "matter_query",
                    matter_id=matter_id,
                    task=task,
                    model_requested=model_requested,
                    model_used=result.model,
                    model_coerced=result.model != model_requested,
                    retrieved_file_ids=result.retrieved_file_ids,
                    # Only when the lawyer narrowed the scope: on the default
                    # path this stays absent, so existing audit records keep
                    # exactly the shape they had before Session 7.
                    **({"scope": scope_mode_used, "scoped_file_ids": scope_ids}
                       if scope_mode_used != "all" else {}),
                )

            matters.touch_last_queried(conn, matter_id)
            cost_status = costs.STATUS_COMPLETED
            cost_row = _finish_cost(
                cost_acc, matter_name_for_cost, cost_started,
                status=cost_status, detail=cost_detail,
            )
            return _render_result(
                result, template, task, structured_inputs,
                back_url=url_for("matter_detail", matter_id=matter_id),
                back_label="Back to matter",
                matter_name=matters.get_matter(conn, matter_id).name,
                scope_mode=scope_mode_used,
                scope_names=scope_names,
                scope_total=scope_total,
                cost=cost_row,
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
        if suffix not in SUPPORTED_SUFFIXES:
            return render_ad_hoc(
                status=400,
                error="Unsupported file type. Upload a .pdf, .docx, .xlsx or .eml file.",
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
        # Ad-hoc runs have no matter, so the cost row carries a NULL matter_id
        # and reads as "(no matter)" in the log. The spend is still the firm's.
        adhoc_started = time.monotonic()
        adhoc_acc = llm.start_cost_run(task=task, matter_id=None,
                                       model=_task_model(task))
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
            if not getattr(adhoc_acc, "_recorded", False):
                _finish_cost(adhoc_acc, None, adhoc_started,
                             status=costs.STATUS_FAILED)
            llm.end_cost_run()

        adhoc_cost = _finish_cost(adhoc_acc, None, adhoc_started,
                                  status=costs.STATUS_COMPLETED)
        return _render_result(
            result, template, task, structured_inputs,
            back_url=url_for("ad_hoc"), back_label="Run another task",
            cost=adhoc_cost,
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
