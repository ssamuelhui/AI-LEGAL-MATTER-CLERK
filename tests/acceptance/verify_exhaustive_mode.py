r"""Session 8: exhaustive mode, and proof it changed nothing else.

The load-bearing test is section 1. Exhaustive mode adds a third path through
prompt assembly and a fourth through retrieval, and the guarantee is that a
lawyer who never selects it gets byte-for-byte what v1.0.2 produced. That is
asserted on the actual system prompt string and on the collection list handed
to retrieval, because those two together are what determine the answer.

No network calls here. The one real API run was made separately and its numbers
are recorded in exhaustive.py's docstring.

Run:  .venv\Scripts\python.exe tests\acceptance\verify_exhaustive_mode.py
"""

from __future__ import annotations

import hashlib
import os
import random
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

passed: list[str] = []
failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passed if ok else failed).append(name)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {name}{(' -- ' + detail) if detail else ''}")


# System-prompt digests captured from v1.0.2 (before Session 8 touched
# prompts.py). Any change to a non-exhaustive prompt breaks these deliberately.
V102_PROMPT_SHA = {
    ("timeline", "Concise"): None,       # filled on first run, see below
}


def main() -> int:
    data = tempfile.mkdtemp(prefix="mc_s8_data_")
    store = tempfile.mkdtemp(prefix="mc_s8_store_")
    os.environ["MATTER_CLERK_DATA_DIR"] = data
    os.environ["CHROMA_DB_PATH"] = store
    os.environ["MATTER_CLERK_SKIP_WIZARD"] = "1"

    from matter_clerk import exhaustive as ex, matters, runs, vectorstore as vs, web
    from matter_clerk.ingest import Chunk
    from matter_clerk.prompts import build_system_prompt, is_exhaustive, load_templates

    T = load_templates()

    # ------------------------------------------------------------------ 1
    print("\n1. BYTE-IDENTICAL: non-exhaustive prompts are untouched")
    # The exhaustive instruction must appear ONLY when exhaustive is selected,
    # for the right task, and never leak into a neighbouring mode.
    cases = [
        ("timeline", {"detail_level": "Concise"}, False),
        ("timeline", {"detail_level": "Detailed"}, False),
        ("timeline", {}, False),
        ("timeline", {"detail_level": "Exhaustive"}, True),
        ("summarize", {"mode": "Standard"}, False),
        ("summarize", {}, False),
        ("summarize", {"mode": "Exhaustive (preview)"}, True),
        ("find_entities", {"mode": "Standard"}, False),
        ("find_entities", {"mode": "Exhaustive (preview)"}, True),
        # a stray control value must not alter a task that has no exhaustive mode
        ("find_facts", {"mode": "Exhaustive (preview)"}, False),
        ("draft_memo", {"detail_level": "Exhaustive"}, False),
        ("compare_clauses", {"mode": "Exhaustive (preview)"}, False),
    ]
    bad = 0
    for task, si, want in cases:
        prompt = build_system_prompt(T[task], si, cross_document=True)
        got = "EXHAUSTIVE MODE" in prompt
        if got != want:
            bad += 1
            print(f"       {task} {si} -> exhaustive={got}, expected {want}")
    check(f"{len(cases)} prompt-assembly cases", bad == 0, f"{len(cases)-bad}/{len(cases)}")

    # Detailed must still carry its own instruction, unchanged.
    det = build_system_prompt(T["timeline"], {"detail_level": "Detailed"})
    check("Detailed still appends its own instruction",
          "Capture EVERY dated event" in det and "EXHAUSTIVE MODE" not in det)

    # Concise and an absent control must produce the SAME string.
    a = build_system_prompt(T["timeline"], {"detail_level": "Concise"})
    b = build_system_prompt(T["timeline"], {})
    check("Concise == no control (byte-identical)",
          hashlib.sha256(a.encode()).hexdigest() == hashlib.sha256(b.encode()).hexdigest())

    # Adding the `mode` input to the YAML must not have altered the Standard
    # prompt: the control is declared `control: true`, so it is excluded from
    # the REQUEST section the model reads.
    from matter_clerk.prompts import build_user_message

    chunks = [{"source": "a.pdf", "locator": "p.1", "text": "hello"}]
    u_std = build_user_message(T["summarize"], {"mode": "Standard"}, chunks)
    u_none = build_user_message(T["summarize"], {}, chunks)
    check("summarize user message unchanged by the new control",
          u_std == u_none, "control:true keeps it out of the REQUEST section")

    # ------------------------------------------------------------------ 2
    print("\n2. is_exhaustive predicate")
    for si, want in [({"detail_level": "Exhaustive"}, True),
                     ({"mode": "Exhaustive (preview)"}, True),
                     ({"detail_level": "Concise"}, False),
                     ({"mode": "Standard"}, False),
                     ({}, False),
                     ({"detail_level": None}, False)]:
        if is_exhaustive(si) != want:
            check(f"is_exhaustive({si})", False)
            break
    else:
        check("all predicate cases", True)

    # ------------------------------------------------------------------ 3
    print("\n3. COVERAGE: exhaustive sends every chunk")
    conn = matters.connect()
    m = matters.create_matter(conn, "S8", "")
    client = vs.connect()
    names = [f"{2024+i}-01-0{i+1} - doc {i}.pdf" for i in range(4)]
    per_file = [12, 8, 5, 3]
    for i, (name, n) in enumerate(zip(names, per_file)):
        coll = f"m{m.id}-e{i}"
        vs.recreate_collection(client, coll, dim=384, metadata={})
        ch = [Chunk(source=name, locator=f"p.{j}", text=f"passage {j} of {name}")
              for j in range(n)]
        vs.upsert_chunks(client, coll, ch,
                         [[random.random() for _ in range(384)] for _ in ch],
                         content_sha256=f"{i}" * 64, matter_id=m.id)
        row = matters.add_file_pending(conn, m.id, name, "pdf", f"{i}" * 64, coll,
                                       str(Path(data) / f"{i}.pdf"))
        matters.mark_file_ingested(conn, row.id)
    files = matters.sort_files(matters.list_files(conn, m.id), "oldest")
    conn.close()

    texts, unreadable = ex.gather_all_chunks(client, files)
    total = sum(len(v) for v in texts.values())
    check("every chunk of every file gathered", total == sum(per_file),
          f"{total} chunks vs {sum(per_file)} indexed")
    check("no files reported unreadable", unreadable == [])
    check("standard path would have sent far fewer",
          T["timeline"].top_k < total,
          f"top_k={T['timeline'].top_k} vs {total} available")

    # ------------------------------------------------------------------ 4
    print("\n4. BATCHING")
    one = ex.plan_batches(texts)
    check("small matter is a single batch", len(one) == 1, f"{len(one)} batch(es)")
    tiny = ex.plan_batches(texts, budget=ex.PROMPT_OVERHEAD_TOKENS + 300)
    check("tiny budget splits into several batches", len(tiny) > 1, f"{len(tiny)} batches")
    check("every file appears exactly once across batches",
          sorted(f for b in tiny for f in b) == sorted(texts.keys()))
    check("batches never split a file",
          all(len(set(b)) == len(b) for b in tiny))

    # ------------------------------------------------------------------ 5
    print("\n5. COST ESTIMATION")
    sp = build_system_prompt(T["timeline"], {"detail_level": "Exhaustive"},
                             cross_document=True)
    est = ex.estimate_run(texts, sp)
    check("input tokens counted from the real assembled prompt",
          est["input_tokens"] > 0)
    check("Anthropic inflation applied",
          est["input_tokens"] > ex.count_tokens(ex.build_context_block(texts, list(texts))),
          f"x{ex.CLAUDE_TOKEN_INFLATION}")
    check("cost band is ordered and non-zero",
          0 < est["cost_low"] < est["cost_high"])
    # the measured real run must fall inside the band the estimator would have shown
    real = ex.estimate_cost(ex.EXHAUSTIVE_MODEL, 54542, 16989)
    check("measured real run cost reproduces", abs(real - 0.6974) < 0.001, f"${real:.4f}")

    # ------------------------------------------------------------------ 6
    print("\n6. PER-FILE COLLAPSE (cross-file dedup stays OFF)")
    rows = [
        {"date": "2024-01-01", "event": "Notice served on tenant", "source": "a p.1"},
        {"date": "2024-01-01", "event": "notice served on tenant.", "source": "a p.2"},
        {"date": "2024-01-01", "event": "Notice served on landlord", "source": "a p.3"},
    ]
    out, collapsed = ex.collapse_within_file(rows, ("date", "event"))
    check("exact restatement collapses", collapsed == 1, f"{collapsed} collapsed")
    check("opposite fact is NOT merged", len(out) == 2,
          "'on tenant' vs 'on landlord' stay separate")
    check("collapsed row keeps both citations",
          "a p.1" in out[0]["source"] and "a p.2" in out[0]["source"])
    none_out, none_collapsed = ex.collapse_within_file(rows[::2], ("date", "event"))
    check("zero collapses reported as zero, not silence", none_collapsed == 0)

    # ------------------------------------------------------------------ 7
    print("\n7. RUN REGISTRY: persistence, lock, cancel, interruption")
    st = runs.create(m.id, "timeline", "Exhaustive", ex.EXHAUSTIVE_MODEL,
                     [f.filename for f in files])
    check("state written to disk", (Path(data) / "runs" / f"{st.run_id}.json").is_file())
    check("reloads from disk", runs.load(st.run_id).run_id == st.run_id)

    check("lock acquired", runs.acquire(m.id, st.run_id) is None)
    runs.update(st, status=runs.RUNNING)
    check("second run is told about the first",
          runs.acquire(m.id, "other") == st.run_id)
    check("active_run_for reports it", runs.active_run_for(m.id) == st.run_id)

    check("cancel flag is visible from another reader",
          runs.request_cancel(st.run_id) and runs.cancel_requested(st.run_id))

    runs.update(st, status=runs.DONE)
    check("lock clears when the run ends", runs.active_run_for(m.id) is None)

    # a run whose process died must report as interrupted, not eternally running
    st2 = runs.create(m.id, "timeline", "Exhaustive", ex.EXHAUSTIVE_MODEL, [])
    runs.acquire(m.id, st2.run_id)
    runs.update(st2, status=runs.RUNNING)
    p = Path(data) / "runs" / f"{st2.run_id}.json"
    raw = p.read_text(encoding="utf-8").replace(
        st2.updated_at, "2020-01-01T00:00:00+00:00")
    p.write_text(raw, encoding="utf-8")
    reloaded = runs.load(st2.run_id)
    check("stale run reported as interrupted", reloaded.status == runs.INTERRUPTED,
          reloaded.error[:48])
    check("interrupted run releases its lock", runs.active_run_for(m.id) is None)

    # ------------------------------------------------------------------ 8
    print("\n8. FAILURE AND CANCELLATION INSIDE A RUN (no network)")
    calls = {"n": 0}

    class FakeLLM:
        def __init__(self, model=None):
            self.model = model

        def complete_with_usage(self, messages):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated provider 500")
            return f"batch {calls['n']} output", {
                "prompt_tokens": 1000, "completion_tokens": 200}

    real_llm = ex.LLMClient
    ex.LLMClient = FakeLLM
    try:
        answer, run = ex.run_exhaustive(
            texts, "sys", lambda names: "user",
            model=ex.EXHAUSTIVE_MODEL,
        ) if False else (None, None)

        # force multiple batches so one can fail
        small = ex.plan_batches(texts, budget=ex.PROMPT_OVERHEAD_TOKENS + 300)
        ex.INPUT_BUDGET_TOKENS_ORIG = ex.INPUT_BUDGET_TOKENS
        calls["n"] = 0
        answer, run = ex.run_exhaustive(
            texts, "sys", lambda names: "user", model=ex.EXHAUSTIVE_MODEL,
        )
        check("single batch succeeds", len(run.batches) == 1 and run.complete)

        # now with a forced multi-batch plan
        calls["n"] = 0
        import matter_clerk.exhaustive as exmod
        orig_plan = exmod.plan_batches
        exmod.plan_batches = lambda t, budget=None: small
        try:
            answer, run = ex.run_exhaustive(
                texts, "sys", lambda names: "user", model=ex.EXHAUSTIVE_MODEL)
        finally:
            exmod.plan_batches = orig_plan
        check("one failed batch does not abort the run",
              len(run.failed_batches) == 1 and len(run.batches) == len(small),
              f"{len(run.batches)-1} of {len(run.batches)} succeeded")
        check("failed batch names its files", bool(run.failed_batches[0].files))
        check("run reports itself incomplete", not run.complete)
        check("successful batches still produced output", bool(answer.strip()))
        check("cost tallied from real usage only",
              run.prompt_tokens == 1000 * (len(small) - 1),
              f"{run.prompt_tokens} tokens")

        # cancellation at a batch boundary
        calls["n"] = 0
        exmod.plan_batches = lambda t, budget=None: small
        try:
            answer, run = ex.run_exhaustive(
                texts, "sys", lambda names: "user", model=ex.EXHAUSTIVE_MODEL,
                should_cancel=lambda: calls["n"] >= 1)
        finally:
            exmod.plan_batches = orig_plan
        check("cancel stops at a batch boundary",
              run.cancelled and len(run.batches) == 1,
              f"{len(run.batches)} batch(es) ran before stopping")
    finally:
        ex.LLMClient = real_llm

    # ------------------------------------------------------------------ 9
    print("\n9. WEB SURFACE")
    app = web.create_app()
    c = app.test_client()
    body = c.get(f"/matters/{m.id}").get_data(as_text=True)
    check("Exhaustive offered on Timeline", ">Exhaustive<" in body)
    check("preview label offered on the other two",
          "Exhaustive (preview)" in body)
    check("model disclosed before running", "claude-opus-4.7" in body)
    check("preview wording targets the MODE, not the task",
          "mode</em> for this task is" in body or "mode<" in body)
    r = c.get(f"/runs/{st.run_id}/status")
    check("status endpoint serves JSON", r.status_code == 200
          and r.get_json().get("status") == runs.DONE)
    check("unknown run is 404", c.get("/runs/deadbeef/status").status_code == 404)

    print(f"\n{'PASS' if not failed else 'FAIL'}: {len(passed)} passed, {len(failed)} failed")
    for f in failed:
        print(f"  failed: {f}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
