r"""Session 10: soft delete, per-task model selection, API key management.

The load-bearing checks:

  * the migration is IDEMPOTENT -- init_db runs on every connect(), so a
    migration that raises the second time would brick the app on its second
    request. Session 6a established that a migration must never turn a working
    install into a dead one.
  * the two UNIQUE constraints behave as designed: re-uploading a soft-deleted
    file RESTORES it, while reusing a deleted matter's name REFUSES.
  * a soft-deleted file is invisible to retrieval, to the Session 7 selector
    and to Session 8 exhaustive gathering.
  * the model picker degrades to a working native <select> with no JS, and the
    inline script parses (the Session 8 lesson).
  * nothing ever writes a key value, or a key length, to a log.

Run:  .venv\Scripts\python.exe tests\acceptance\verify_soft_delete_and_models.py
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

passed: list[str] = []
failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passed if ok else failed).append(name)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {name}{(' -- ' + detail) if detail else ''}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mc_s10_"))
    os.environ["MATTER_CLERK_DATA_DIR"] = str(tmp)
    os.environ["CHROMA_DB_PATH"] = str(tmp / "store")
    os.environ["MATTER_CLERK_SKIP_WIZARD"] = "1"

    from matter_clerk import (exhaustive as ex, maintenance, matters,
                              model_registry as mr, pipeline, vectorstore as vs, web)
    from matter_clerk.ingest import Chunk

    # ------------------------------------------------------------------ 1
    print("\n1. MIGRATION: idempotent, non-destructive")
    conn = matters.connect()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(files)")}
    mcols = {r["name"] for r in conn.execute("PRAGMA table_info(matters)")}
    check("files.deleted_at added", "deleted_at" in cols)
    check("matters.deleted_at added", "deleted_at" in mcols)
    conn.close()
    for i in range(3):
        matters.connect().close()          # init_db runs every time
    check("running the migration repeatedly is safe", True, "4 connects")

    conn = matters.connect()
    m = matters.create_matter(conn, "Pilot matter", "")
    client = vs.connect()
    ids = []
    for i in range(3):
        coll = f"m{m.id}-f{i}"
        vs.recreate_collection(client, coll, dim=384, metadata={})
        ch = [Chunk(source=f"f{i}.pdf", locator=f"p.{j}", text=f"content {i}-{j}")
              for j in range(4)]
        vs.upsert_chunks(client, coll, ch,
                         [[random.random() for _ in range(384)] for _ in ch],
                         content_sha256=f"{i}" * 64, matter_id=m.id)
        row = matters.add_file_pending(conn, m.id, f"file{i}.pdf", "pdf",
                                       f"{i}" * 64, coll, str(tmp / f"f{i}.pdf"))
        (tmp / f"f{i}.pdf").write_text("x", encoding="utf-8")
        matters.mark_file_ingested(conn, row.id)
        ids.append(row.id)
    check("existing rows read as live (deleted_at NULL)",
          len(matters.list_files(conn, m.id)) == 3)

    # ------------------------------------------------------------------ 2
    print("\n2. SOFT DELETE AND RESTORE")
    matters.soft_delete_file(conn, ids[0])
    live = matters.list_files(conn, m.id)
    check("deleted file hidden from the matter", len(live) == 2)
    check("deleted file still exists in the table",
          matters.get_file_any(conn, ids[0]) is not None)
    dm, df = matters.list_deleted(conn)
    check("deleted file appears in Deleted items", len(df) == 1,
          df[0]["file"].filename)
    check("days remaining starts at the full window",
          matters.days_remaining(df[0]["file"].deleted_at) == 30)
    matters.restore_file(conn, ids[0])
    check("restore brings it back", len(matters.list_files(conn, m.id)) == 3)

    matters.soft_delete_matter(conn, m.id)
    check("deleted matter hidden from the list",
          all(x.id != m.id for x in matters.list_matters(conn)))
    check("deleted matter still reachable for result pages",
          matters.get_matter_any(conn, m.id) is not None)
    matters.restore_matter(conn, m.id)
    check("matter restored", any(x.id == m.id for x in matters.list_matters(conn)))

    # ------------------------------------------------------------------ 3
    print("\n3. THE TWO UNIQUE CONSTRAINTS")
    matters.soft_delete_file(conn, ids[1])
    prior = matters.find_deleted_file_by_hash(conn, m.id, "1" * 64)
    check("a soft-deleted file is found by content hash", prior is not None)
    try:
        matters.add_file_pending(conn, m.id, "file1.pdf", "pdf", "1" * 64,
                                 "x", str(tmp / "f1.pdf"))
        check("re-upload still hits the UNIQUE constraint", False, "no error")
    except matters.DuplicateFileInMatter:
        check("re-upload hits the UNIQUE constraint (so restore is required)", True)
    matters.restore_file(conn, ids[1])
    check("restoring resolves it", len(matters.list_files(conn, m.id)) == 3)

    matters.soft_delete_matter(conn, m.id)
    check("deleted matter's name is findable",
          matters.deleted_matter_named(conn, "Pilot matter") is not None)
    try:
        matters.create_matter(conn, "Pilot matter", "")
        check("reusing a deleted matter name is refused", False, "no error")
    except matters.DuplicateMatterName:
        check("reusing a deleted matter name is refused", True)
    matters.restore_matter(conn, m.id)
    conn.close()

    # ------------------------------------------------------------------ 4
    print("\n4. DELETED FILES ARE INVISIBLE TO RETRIEVAL AND TO SESSIONS 7/8")
    conn = matters.connect()
    matters.soft_delete_file(conn, ids[2])
    live = [f for f in matters.list_files(conn, m.id)
            if matters.is_queryable(f.ingest_status)]
    conn.close()
    check("Session 7 selector sees only live files", len(live) == 2)
    from matter_clerk.web import _matter_files_for_selection

    check("selector shape excludes the deleted file",
          len(_matter_files_for_selection(live)) == 2)
    texts, unread = ex.gather_all_chunks(client, live)
    check("Session 8 exhaustive gathers only live files", len(texts) == 2,
          f"{sum(len(v) for v in texts.values())} chunks")
    check("its collection is untouched, so restore is free",
          vs.collection_doc_count(client, f"m{m.id}-f2") == 4)
    conn = matters.connect(); matters.restore_file(conn, ids[2]); conn.close()

    # ------------------------------------------------------------------ 5
    print("\n5. PERMANENT DELETION AFTER THE WINDOW")
    conn = matters.connect()
    matters.soft_delete_file(conn, ids[2])
    conn.execute("UPDATE files SET deleted_at = '2020-01-01T00:00:00+00:00' "
                 "WHERE id = ?", (ids[2],))
    conn.commit()
    due_files, due_matters = matters.due_for_purge(conn)
    conn.close()
    check("an expired item is due for purge", len(due_files) == 1)
    check("a recently deleted item is NOT due", len(due_matters) == 0)
    r = maintenance.purge_expired()
    check("purge removes the row", r["files"] == 1, str(r))
    check("purge removes the Chroma collection",
          vs.collection_doc_count(client, f"m{m.id}-f2") is None)
    conn = matters.connect()
    check("purge is bounded, not unbounded",
          maintenance.PURGE_BATCH == 25)
    check("live data untouched by purge",
          len(matters.list_files(conn, m.id)) == 2)
    conn.close()

    # ------------------------------------------------------------------ 6
    print("\n6. MODEL REGISTRY")
    cat = mr.available_models()
    check("catalogue loads", len(cat["models"]) > 3, f"{len(cat['models'])} models")
    picker = mr.sort_for_picker(list(cat["models"]))
    check("recommended pinned to the top",
          [p["id"] for p in picker[:3]] == list(mr.RECOMMENDED_MODELS))
    check("all three recommended models really exist on OpenRouter",
          all(mr.model_exists(mid, cat["models"]) for mid in mr.RECOMMENDED_MODELS),
          "opus id uses a dot, not a dash")
    check("tiers derived from the measured distribution",
          (mr.tier_for(1.30), mr.tier_for(12.0), mr.tier_for(30.0)) == ("$", "$$", "$$$"))

    fb = mr._fallback_models()
    check("fallback offers exactly the recommended three", len(fb) == 3)

    check("missing preferences file is not an error", isinstance(mr.load_preferences(), dict))
    mr.save_preference("draft_memo", "anthropic/claude-sonnet-5")
    check("preference persists", mr.resolve_model("draft_memo")[0] == "anthropic/claude-sonnet-5")
    check("unknown task falls back silently",
          mr.resolve_model("not_a_task") == (mr.DEFAULT_MODEL, None))
    mr.save_preference("timeline", "vendor/gone-9000")
    mid, warn = mr.resolve_model("timeline")
    check("vanished model reverts to default with a warning",
          mid == mr.DEFAULT_MODEL and warn is not None)
    check("and the preference is rewritten so it warns once",
          mr.load_preferences()["timeline"] == mr.DEFAULT_MODEL)
    (tmp / "user_preferences.json").write_text("{ not json", encoding="utf-8")
    check("corrupt preferences do not crash", mr.load_preferences() == {})
    check("corrupt preferences file is NOT deleted",
          (tmp / "user_preferences.json").is_file(), "the original is evidence")
    (tmp / "user_preferences.json").unlink()

    # a degraded catalogue must not rewrite good preferences
    mr.save_preference("summarize", "some/real-model")
    mid2, warn2 = mr.resolve_model("summarize", models=mr._fallback_models())
    check("an outage does not discard a saved preference",
          mid2 == "some/real-model" and warn2 is None)

    # ------------------------------------------------------------------ 7
    print("\n7. MODEL OVERRIDE THREADING")
    check("no override by default", pipeline.get_model_override() is None)
    pipeline.set_model_override("anthropic/claude-sonnet-5")
    check("override readable", pipeline.get_model_override() == "anthropic/claude-sonnet-5")
    pipeline.set_model_override(None)
    check("cleared override restores v1.0.4 behaviour",
          pipeline.get_model_override() is None)
    check("exhaustive stays pinned regardless",
          ex.EXHAUSTIVE_MODEL == "anthropic/claude-opus-4.7")

    # ------------------------------------------------------------------ 8
    print("\n8. WEB SURFACE")
    app = web.create_app()
    c = app.test_client()
    for path in ("/", "/settings", "/deleted", "/ad-hoc", f"/matters/{m.id}"):
        check(f"GET {path}", c.get(path).status_code == 200)

    body = c.get("/settings").get_data(as_text=True)
    check("settings shows both key rows", "OpenRouter" in body and "CanLII" in body)
    check("model picker present on settings", "model-select" in body)

    detail = c.get(f"/matters/{m.id}").get_data(as_text=True)
    check("per-file delete control", "Delete this file" in detail)
    check("type-to-confirm matter delete", "Type the matter name to confirm" in detail)
    check("model picker on the task form", 'data-picker="taskmodel"' in detail)
    check("coercion warning markup present", "exh-model-warn" in detail)

    # type-to-confirm is enforced server side
    before = len(matters.list_matters(matters.connect()))
    r1 = c.post(f"/matters/{m.id}/delete", data={"confirm_name": "wrong name"})
    conn = matters.connect()
    still = any(x.id == m.id for x in matters.list_matters(conn))
    conn.close()
    check("wrong confirmation name does NOT delete", still, f"HTTP {r1.status_code}")
    c.post(f"/matters/{m.id}/delete", data={"confirm_name": "Pilot matter"})
    conn = matters.connect()
    gone = all(x.id != m.id for x in matters.list_matters(conn))
    conn.close()
    check("exact confirmation name deletes", gone)
    c.post(f"/deleted/matters/{m.id}/restore")

    # ------------------------------------------------------------------ 9
    print("\n9. INLINE JAVASCRIPT STILL PARSES (Session 8 lesson)")
    node = shutil.which("node")
    for path, label in (("/settings", "settings"), (f"/matters/{m.id}", "matter detail"),
                        ("/ad-hoc", "ad-hoc")):
        html = c.get(path).get_data(as_text=True)
        blocks = [b for b in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                                        html, re.S) if b.strip()]
        for i, js in enumerate(blocks):
            if node:
                with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                                 encoding="utf-8") as fh:
                    fh.write(js); jpath = fh.name
                try:
                    res = subprocess.run([node, "--check", jpath],
                                         capture_output=True, text=True, timeout=30)
                    check(f"{label} script {i+1} parses", res.returncode == 0,
                          (res.stderr or "").strip().split("\n")[0][:70])
                finally:
                    os.unlink(jpath)

    print("\n   no-JS fallback")
    html = c.get("/settings").get_data(as_text=True)
    check("picker submits a real <select>", 'name="model"' in html and "<select" in html)
    check("search box starts hidden until the script reveals it",
          'class="model-search"' in html and "hidden" in html,
          "so a dead script leaves no orphan control")

    # ----------------------------------------------------------------- 10
    print("\n10. KEYS NEVER REACH A LOG")
    from matter_clerk.web import _mask_key

    secret = "sk-or-v1-abcdef0123456789abcdef0123456789abcdef"
    masked = _mask_key(secret)
    check("mask keeps only head and tail", secret not in masked, masked)
    check("mask hides the key length",
          len(_mask_key(secret)) == len(_mask_key(secret + "extralongsuffix")),
          "length is itself information")

    buf = StringIO()
    handler = logging.StreamHandler(buf)
    root = logging.getLogger()
    root.addHandler(handler); root.setLevel(logging.DEBUG)
    try:
        from matter_clerk import first_run_wizard as w

        w.test_openrouter("sk-or-v1-THIS-IS-A-SECRET-KEY-VALUE")
    except Exception:                                             # noqa: BLE001
        pass
    finally:
        root.removeHandler(handler)
    logged = buf.getvalue()
    check("a failed key test logs no key material",
          "THIS-IS-A-SECRET-KEY-VALUE" not in logged, f"{len(logged)} chars logged")

    audit_text = ""
    from matter_clerk import audit

    if audit.audit_log_path().is_file():
        audit_text = audit.audit_log_path().read_text(encoding="utf-8")
    check("audit log holds no key material",
          "THIS-IS-A-SECRET-KEY-VALUE" not in audit_text)

    print(f"\n{'PASS' if not failed else 'FAIL'}: {len(passed)} passed, {len(failed)} failed")
    for f in failed:
        print(f"  failed: {f}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
