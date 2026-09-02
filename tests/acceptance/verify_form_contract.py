r"""The form's two silent contracts (Session 8 hotfix).

Both failures this guards against are invisible from the server: the page
returns 200, the markup contains everything you would grep for, and nothing
raises. Only a human clicking the form finds out.

1. INLINE JAVASCRIPT MUST PARSE.
   v1.0.3 shipped with a stray line break inside a string literal in the
   exhaustive confirmation dialog. The browser discarded the whole <script>
   block, so task switching, the file selector panes, the exhaustive note and
   the authority radios all stopped working -- and the form froze showing only
   the default task's single question box. Python was happy, Jinja was happy,
   every existing test was happy, because they all assert on markup.

2. EVERY YAML-DECLARED INPUT MUST REACH structured_inputs.
   If a task declares an input the collector does not read, the control renders,
   the lawyer sets it, and the run quietly ignores it. Nothing errors and the
   output looks plausible. This binds the schema to the collector so the two
   cannot drift apart unnoticed.

Run:  .venv\Scripts\python.exe tests\acceptance\verify_form_contract.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

passed: list[str] = []
failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passed if ok else failed).append(name)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {name}{(' -- ' + detail) if detail else ''}")


# --------------------------------------------------------------------------
# 1. JavaScript syntax
# --------------------------------------------------------------------------
def _unterminated_string_lines(js: str) -> list[tuple[int, str]]:
    """Lines holding an odd number of unescaped double quotes.

    The pure-Python fallback for machines without node. It catches exactly the
    bug that shipped -- a string literal opened and not closed on its line --
    without needing a real parser. Lines inside block comments are skipped.
    """
    bad: list[tuple[int, str]] = []
    in_block = False
    for n, line in enumerate(js.split("\n"), start=1):
        stripped = line.strip()
        if in_block:
            if "*/" in stripped:
                in_block = False
            continue
        if stripped.startswith("/*"):
            in_block = "*/" not in stripped
            continue
        if stripped.startswith("//"):
            continue
        code = line.split("//")[0]
        quotes = len(re.findall(r'(?<!\\)"', code))
        if quotes % 2 == 1:
            bad.append((n, line.strip()[:70]))
    return bad


def check_js(label: str, js: str) -> None:
    node = shutil.which("node")
    if node:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(js)
            path = fh.name
        try:
            r = subprocess.run([node, "--check", path], capture_output=True,
                               text=True, timeout=30)
            ok = r.returncode == 0
            first = (r.stderr or "").strip().split("\n")
            detail = "" if ok else next(
                (ln for ln in first if "Error" in ln), first[0] if first else "")
            check(f"{label}: parses (node --check)", ok, detail)
        finally:
            os.unlink(path)
    else:
        bad = _unterminated_string_lines(js)
        check(f"{label}: no unterminated string literals (no node; heuristic)",
              not bad, "; ".join(f"line {n}: {t}" for n, t in bad[:3]))


def main() -> int:
    data = tempfile.mkdtemp(prefix="mc_form_")
    store = tempfile.mkdtemp(prefix="mc_formstore_")
    os.environ["MATTER_CLERK_DATA_DIR"] = data
    os.environ["CHROMA_DB_PATH"] = store
    os.environ["MATTER_CLERK_SKIP_WIZARD"] = "1"

    from matter_clerk import matters, runs, vectorstore as vs, web
    from matter_clerk.ingest import Chunk
    from matter_clerk.prompts import load_templates
    from matter_clerk.web import _collect_web_inputs
    from werkzeug.datastructures import MultiDict

    T = load_templates()

    # a matter with real files so the selector renders with content
    conn = matters.connect()
    m = matters.create_matter(conn, "Form contract", "")
    client = vs.connect()
    for i in range(3):
        coll = f"m{m.id}-c{i}"
        vs.recreate_collection(client, coll, dim=384, metadata={})
        ch = [Chunk(source=f"{i}.pdf", locator=f"p.{j}", text=f"t{j}") for j in range(3)]
        vs.upsert_chunks(client, coll, ch, [[0.1] * 384 for _ in ch],
                         content_sha256=f"{i}" * 64, matter_id=m.id)
        row = matters.add_file_pending(conn, m.id, f"202{i}-01-0{i+1} - doc.pdf",
                                       "pdf", f"{i}" * 64, coll,
                                       str(Path(data) / f"{i}.pdf"))
        matters.mark_file_ingested(conn, row.id)
    conn.close()

    app = web.create_app()
    c = app.test_client()

    print("\n1. INLINE JAVASCRIPT PARSES ON EVERY PAGE THAT SHIPS IT")
    st = runs.create(m.id, "timeline", "Exhaustive", "anthropic/claude-opus-4.7", [])
    runs.update(st, status=runs.RUNNING)
    pages = [
        (f"/matters/{m.id}", "matter detail"),
        ("/ad-hoc", "ad-hoc form"),
        ("/", "matters list"),
        (f"/runs/{st.run_id}", "run page"),
    ]
    total_blocks = 0
    for url, label in pages:
        body = c.get(url).get_data(as_text=True)
        blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", body, re.S)
        blocks = [b for b in blocks if b.strip()]
        total_blocks += len(blocks)
        for i, js in enumerate(blocks):
            check_js(f"{label} script {i + 1}", js)
    check("every page carrying inline script was checked", total_blocks > 0,
          f"{total_blocks} block(s)")

    print("\n2. THE REGRESSION THAT SHIPPED IN v1.0.3")
    body = c.get(f"/matters/{m.id}").get_data(as_text=True)
    js = max(re.findall(r"<script>(.*?)</script>", body, re.S), key=len)
    check("no stray line break inside a string literal",
          not _unterminated_string_lines(js),
          "; ".join(f"line {n}" for n, _ in _unterminated_string_lines(js)[:3]))
    # the specific control surfaces that died when the script did
    for probe, what in [("function update(", "task switching"),
                        ("updateScopePanes", "file-selector panes"),
                        ("updateExhaustiveNote", "exhaustive note"),
                        ("updateCompareGate", "compare-clauses gate")]:
        check(f"{what} present in the shipped script", probe in js)

    print("\n3. EVERY YAML-DECLARED INPUT REACHES structured_inputs")
    # Binds the schema to the collector. A task that declares an input the
    # collector cannot read is a control the lawyer sets and the run ignores.
    for tid, template in sorted(T.items()):
        form = MultiDict()
        expect: dict = {}
        for f in template.inputs:
            if f.type in ("multiselect", "file_multiselect"):
                form.setlist(f.name, ["11", "22"])
                expect[f.name] = ["11", "22"]
            elif f.type == "checkbox":
                form[f.name] = "yes"
                expect[f.name] = True
            elif f.type in ("select", "radio") and f.options:
                form[f.name] = f.options[-1]
                expect[f.name] = f.options[-1]
            else:
                form[f.name] = f"v_{f.name}"
                expect[f.name] = f"v_{f.name}"
        got = _collect_web_inputs(template, form)
        missing = sorted(set(expect) - set(got))
        wrong = {k: (expect[k], got[k]) for k in expect if k in got and got[k] != expect[k]}
        check(f"{tid}: all {len(template.inputs)} declared inputs collected",
              not missing and not wrong,
              f"missing={missing} wrong={wrong}" if (missing or wrong) else "")

    print("\n4. CONTROLS THE TEMPLATES DEPEND ON ARE STILL DECLARED")
    # Names referenced by code, not just by YAML. If a template is renamed or an
    # option string drifts, the control renders and silently does nothing.
    required = {
        "draft_pleading": ["pleading_type", "claim_particulars", "authority_mode"],
        "suggest_cases": ["jurisdiction", "max_cases"],
        "timeline": ["detail_level"],
        "summarize": ["mode"],
        "find_entities": ["mode"],
        "compare_clauses": ["clauses_to_compare"],
    }
    for tid, names in required.items():
        declared = {f.name for f in T[tid].inputs}
        gone = [n for n in names if n not in declared]
        check(f"{tid} declares {', '.join(names)}", not gone, f"missing {gone}")

    from matter_clerk.prompts import AUTHORITY_MODE_ON, authority_mode_enabled
    opts = next(f.options for f in T["draft_pleading"].inputs
                if f.name == "authority_mode")
    check("authority_mode option string still matches AUTHORITY_MODE_ON",
          AUTHORITY_MODE_ON in opts,
          f"gate expects {AUTHORITY_MODE_ON!r}")
    check("authority mode turns ON when selected",
          authority_mode_enabled("draft_pleading",
                                 {"authority_mode": AUTHORITY_MODE_ON}))
    check("authority mode stays OFF for tasks that do not support it",
          not authority_mode_enabled("timeline",
                                     {"authority_mode": AUTHORITY_MODE_ON}))

    print(f"\n{'PASS' if not failed else 'FAIL'}: {len(passed)} passed, {len(failed)} failed")
    for f in failed:
        print(f"  failed: {f}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
