r"""Session 11: per-task cost tracking for firm billing.

The two checks that carry the most weight:

  SECTION 2 -- a task making SEVERAL model calls records their SUM, once.
  `discovery` makes two calls per run (concept extraction, then case notes), so
  a per-call-site hook would have under-counted Suggest Relevant Cases by half
  with no symptom. The accumulator lives inside LLMClient precisely so a call
  site cannot be forgotten.

  SECTION 6 -- the v1.0.5 pricing bug. `exhaustive.MODEL_PRICING` held three
  models with an Opus-rate fallback while Session 10 made 425 selectable, so
  the pre-run dialog mispriced almost everything. Asserted in both directions:
  a cheap model was quoted far too high, an expensive one far too low.

Most of this runs offline against a stubbed client. One real API call is made
against the cheapest model in the catalogue (fractions of a cent) because a
stub cannot prove that `extra_body={"usage": {"include": True}}` actually
reaches OpenRouter and comes back with a cost.

Run:  .venv\Scripts\python.exe tests\acceptance\verify_cost_tracking.py
"""

from __future__ import annotations

import csv
import io
import json
import os
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


class FakeResponse:
    """Shaped like an OpenAI SDK response, including OpenRouter's cost field."""

    def __init__(self, text, prompt, completion, cost, model):
        self.choices = [type("C", (), {"message": type("M", (), {"content": text})()})()]
        self.model = model
        self.usage = type("U", (), {
            "model_dump": lambda self_: {
                "prompt_tokens": prompt, "completion_tokens": completion,
                "total_tokens": prompt + completion, "cost": cost,
            }
        })()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mc_s11_"))
    os.environ["MATTER_CLERK_DATA_DIR"] = str(tmp)
    os.environ["CHROMA_DB_PATH"] = str(tmp / "store")
    os.environ["MATTER_CLERK_SKIP_WIZARD"] = "1"
    # Load the real key first; the placeholder is only for a machine without one.
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:                                             # noqa: BLE001
        pass
    os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-not-used-offline")

    from matter_clerk import costs, exhaustive as ex, llm, maintenance, matters

    # ------------------------------------------------------------------ 1
    print("\n1. THE ACCUMULATOR LIVES IN THE CLIENT")
    acc = llm.start_cost_run(task="draft_memo", matter_id=7, model="m/x")
    check("scope is visible to any client on this thread",
          llm.current_run() is acc)
    acc.add({"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.01,
             "model": "m/x"})
    check("one call recorded", (acc.calls, acc.input_tokens, acc.output_tokens)
          == (1, 100, 50))
    llm.end_cost_run()
    check("scope closes", llm.current_run() is None)

    # ------------------------------------------------------------------ 2
    print("\n2. MULTI-CALL RUNS SUM, AND ARE NOT DOUBLE-COUNTED")
    acc = llm.start_cost_run(task="suggest_cases", matter_id=1, model="m/x")
    # exactly what discovery does: concept extraction, then case notes
    acc.add({"prompt_tokens": 2000, "completion_tokens": 400, "cost": 0.03,
             "model": "m/x"})
    acc.add({"prompt_tokens": 1500, "completion_tokens": 900, "cost": 0.05,
             "model": "m/x"})
    check("two calls counted as two", acc.calls == 2)
    check("tokens summed", (acc.input_tokens, acc.output_tokens) == (3500, 1300))
    check("cost summed, not averaged or replaced",
          abs(acc.cost_usd - 0.08) < 1e-9, f"${acc.cost_usd}")
    llm.end_cost_run()

    print("   and through a real LLMClient with a stubbed transport")
    calls = {"n": 0}

    class StubOpenAI:
        def __init__(self, *a, **k):
            self.chat = type("Chat", (), {"completions": self})()

        def create(self, **kwargs):
            calls["n"] += 1
            # the flag that makes OpenRouter report cost at all
            assert kwargs.get("extra_body", {}).get("usage", {}).get("include") is True
            return FakeResponse("answer", 1000, 200, 0.02, kwargs["model"])

    real_openai = llm.OpenAI
    llm.OpenAI = StubOpenAI
    try:
        client = llm.LLMClient(api_key="k", model="vendor/model")
        acc = llm.start_cost_run(task="t", matter_id=2, model="vendor/model")
        client.complete([{"role": "user", "content": "a"}])
        client.complete([{"role": "user", "content": "b"}])
        check("usage.include is sent on every call", calls["n"] == 2)
        check("both calls accumulated", acc.calls == 2 and acc.input_tokens == 2000)
        check("cost is the sum", abs(acc.cost_usd - 0.04) < 1e-9)
        check("model captured from the response", acc.models_used == ["vendor/model"])
        llm.end_cost_run()

        print("\n   a response with no cost field yields Unknown, not a wrong total")

        class NoCostOpenAI(StubOpenAI):
            def create(self, **kwargs):
                return FakeResponse("x", 10, 5, None, kwargs["model"])

        llm.OpenAI = NoCostOpenAI
        client2 = llm.LLMClient(api_key="k", model="vendor/model")
        acc2 = llm.start_cost_run(task="t", matter_id=2, model="vendor/model")
        client2.complete([{"role": "user", "content": "a"}])
        check("cost_unavailable flagged", acc2.cost_unavailable is True)
        check("tokens still recorded", acc2.input_tokens == 10)
        row_id = costs.record_from_accumulator(
            acc2, matter_name=None, duration_seconds=1.0)
        conn = costs.connect()
        row = conn.execute("SELECT * FROM task_costs WHERE id = ?", (row_id,)).fetchone()
        conn.close()
        check("recorded as NULL cost rather than a short total",
              row["cost_usd"] is None, "a quietly low figure is worse for billing")
        llm.end_cost_run()
    finally:
        llm.OpenAI = real_openai

    # ------------------------------------------------------------------ 3
    print("\n3. ROWS FOR COMPLETED, FAILED AND CANCELLED RUNS")
    conn = matters.connect()
    m = matters.create_matter(conn, "Imperial Plaza", "")
    m2 = matters.create_matter(conn, "Cresthaven", "")
    conn.close()

    costs.record(task_id="draft_memo", matter_id=m.id, matter_name=m.name,
                 model_used="anthropic/claude-opus-4.7", input_tokens=4232,
                 output_tokens=1847, cost_usd=0.47, duration_seconds=8.3)
    costs.record(task_id="timeline", matter_id=m.id, matter_name=m.name,
                 model_used="anthropic/claude-opus-4.7", input_tokens=54542,
                 output_tokens=16989, cost_usd=0.6974, duration_seconds=169.5,
                 was_exhaustive=True)
    costs.record(task_id="summarize", matter_id=m2.id, matter_name=m2.name,
                 model_used="xiaomi/mimo-v2.5-pro", input_tokens=800,
                 output_tokens=100, cost_usd=0.0012, duration_seconds=3.1,
                 status=costs.STATUS_FAILED, detail="failed after 3s")
    costs.record(task_id="timeline", matter_id=m2.id, matter_name=m2.name,
                 model_used="anthropic/claude-opus-4.7", input_tokens=20000,
                 output_tokens=5000, cost_usd=0.31, duration_seconds=64.0,
                 was_exhaustive=True, status=costs.STATUS_CANCELLED,
                 detail="cancelled at batch 8 of 12")
    costs.record(task_id="find_facts", matter_id=m.id, matter_name=m.name,
                 model_used=None, input_tokens=0, output_tokens=0,
                 cost_usd=0.0, duration_seconds=0.2,
                 status=costs.STATUS_FAILED, detail="failed before any model call")

    conn = costs.connect()
    rows = costs.query(conn, period="all")
    # 5 written here, plus the NULL-cost row section 2 recorded on purpose.
    check("all runs recorded", len(rows) == 6, f"{len(rows)} rows")
    by_status = {r["status"] for r in rows}
    check("completed, failed and cancelled all present",
          by_status == {"completed", "failed", "cancelled"}, str(sorted(by_status)))
    zero = [r for r in rows if r["task_id"] == "find_facts"][0]
    check("a run that died before the model still leaves a $0.00 row",
          zero["cost_usd"] == 0.0 and zero["status"] == "failed",
          "so a vanished task is findable")

    total, count, unknown = costs.totals(rows)
    check("filtered total sums every status",
          abs(total - (0.47 + 0.6974 + 0.0012 + 0.31 + 0.0)) < 1e-6, f"${total}")

    # ------------------------------------------------------------------ 4
    print("\n4. FILTERS, INCLUDING DELETED AND PURGED MATTERS")
    only_m = costs.query(conn, matter_id=m.id, period="all")
    check("filter by matter", len(only_m) == 3, f"{len(only_m)} rows")
    opts = costs.matter_options(conn)
    check("both matters offered in the filter", len(opts) == 2)

    mc = matters.connect()
    matters.soft_delete_matter(mc, m2.id)
    mc.close()
    rows = costs.query(conn, period="all")
    labels = {r["display_matter"] for r in rows}
    check("a soft-deleted matter still shows its spending",
          "Cresthaven (deleted)" in labels, str(sorted(labels)))
    opts = costs.matter_options(conn)
    check("and still appears in the filter dropdown",
          any("(deleted)" in o["label"] for o in opts))

    mc = matters.connect()
    matters.hard_delete_matter(mc, m2.id)
    mc.close()
    rows = costs.query(conn, period="all")
    labels = {r["display_matter"] for r in rows}
    check("a PURGED matter is still readable by its stored name",
          "Cresthaven (removed)" in labels,
          "which is why matter_name is denormalised onto the row")
    check("its cost rows survive the purge",
          len([r for r in rows if "Cresthaven" in r["display_matter"]]) == 2,
          "billing history outlives the matter")

    check("sort by cost descending",
          [r["cost_usd"] for r in costs.query(conn, period="all", sort="cost",
                                              direction="desc")][0] == 0.6974)
    check("period filter keeps rows written just now",
          len(costs.query(conn, period="7")) == 6, "all just written")

    # ------------------------------------------------------------------ 5
    print("\n5. CSV EXPORT")
    text = costs.to_csv(costs.query(conn, period="all"))
    parsed = list(csv.reader(io.StringIO(text)))
    check("header matches the documented column order",
          parsed[0] == costs.CSV_COLUMNS, str(parsed[0][:4]))
    check("one row per record", len(parsed) == 7, f"{len(parsed)-1} data rows")
    widths = {len(r) for r in parsed}
    check("every row has the same column count", len(widths) == 1, str(widths))
    cost_col = costs.CSV_COLUMNS.index("cost_usd")
    check("cost is a plain 4dp decimal with no symbol",
          all(r[cost_col] == "" or (r[cost_col].replace(".", "").isdigit()
                                    and len(r[cost_col].split(".")[1]) == 4)
              for r in parsed[1:]),
          "sub-cent runs must not round to zero")
    name_col = costs.CSV_COLUMNS.index("matter_name")
    check("purged matter still named in the export",
          any("Cresthaven" in r[name_col] for r in parsed[1:]))
    check("filename is dated", costs.csv_filename().startswith("task_costs_"))
    conn.close()

    # ------------------------------------------------------------------ 6
    print("\n6. THE v1.0.5 PRICING BUG IS FIXED (pre-run estimator)")
    check("MODEL_PRICING no longer exists", not hasattr(ex, "MODEL_PRICING"))
    cheap_in, cheap_out = ex.pricing_for("mistralai/mistral-nemo")
    check("a cheap model is no longer quoted at Opus rates",
          cheap_in < 1.0 and cheap_out < 1.0,
          f"${cheap_in}/${cheap_out}, v1.0.5 said $5.00/$25.00")
    dear_in, dear_out = ex.pricing_for("openai/o1-pro")
    check("an expensive model is no longer quoted far too LOW",
          dear_in > 25.0,
          f"${dear_in}/${dear_out}, v1.0.5 said $5.00/$25.00")
    opus_in, opus_out = ex.pricing_for("anthropic/claude-opus-4.7")
    check("Opus still priced correctly", (opus_in, opus_out) == (5.00, 25.00))
    unknown_in, unknown_out = ex.pricing_for("nobody/nothing-at-all")
    check("an unknown model falls back conservatively HIGH",
          (unknown_in, unknown_out) == ex.DEFAULT_PRICING,
          "too high makes a lawyer hesitate; too low spends their money")

    # ------------------------------------------------------------------ 7
    print("\n7. BACKFILL FROM audit.jsonl")
    logs = tmp / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    entries = [
        {"timestamp": "2026-09-01T16:00:29+00:00", "event": "matter_query",
         "matter_id": m.id, "task": "timeline", "exhaustive": True,
         "model": "anthropic/claude-opus-4.7", "prompt_tokens": 4258,
         "completion_tokens": 2287, "cost_usd": 0.0785, "seconds": 23.2,
         "cancelled": False, "run_id": "abc123"},
        # the zeroed run from before the Session 8 tally fix
        {"timestamp": "2026-09-01T15:00:00+00:00", "event": "matter_query",
         "matter_id": m.id, "task": "timeline", "exhaustive": True,
         "model": "anthropic/claude-opus-4.7", "prompt_tokens": 0,
         "completion_tokens": 0, "cost_usd": 0.0, "seconds": 0.0},
        # a non-exhaustive run: no cost was ever recorded for these
        {"timestamp": "2026-08-30T10:00:00+00:00", "event": "matter_query",
         "matter_id": m.id, "task": "find_facts"},
        {"timestamp": "2026-08-30T10:05:00+00:00", "event": "limitation_review"},
    ]
    (logs / "audit.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

    before = len(costs.query(costs.connect(), period="all"))
    r = maintenance.backfill_costs_from_audit()
    check("scans every line", r["scanned"] == 4, str(r))
    check("recovers the one real cost-bearing run", r["inserted"] == 1)
    check("skips the zeroed pre-fix run", r["skipped"] == 1,
          "a $0.00 exhaustive row would be noise, not history")
    conn = costs.connect()
    rows = costs.query(conn, period="all")
    back = [x for x in rows if x["source"] == "backfill"]
    check("tagged source=backfill",
          len(back) == 1 and back[0]["cost_usd"] == 0.0785,
          "so a reconstructed figure is distinguishable from a measured one")
    check("original timestamp preserved",
          back[0]["timestamp"].startswith("2026-09-01"))
    conn.close()

    r2 = maintenance.backfill_costs_from_audit()
    check("re-running inserts nothing", r2["inserted"] == 0, str(r2))

    # ------------------------------------------------------------------ 8
    print("\n8. WEB SURFACE")
    from matter_clerk import web

    app = web.create_app()
    c = app.test_client()
    for path in ("/", "/settings", "/costs", "/deleted", "/ad-hoc"):
        check(f"GET {path}", c.get(path).status_code == 200)

    body = c.get("/costs").get_data(as_text=True)
    check("log lists task runs", "Imperial Plaza" in body)
    check("filtered total shown", "Filtered total" in body)
    check("exhaustive runs flagged", "exhaustive" in body)
    check("failed and cancelled runs annotated",
          "cancelled at batch 8 of 12" in body and "failed after 3s" in body)
    check("CanLII exclusion stated", "CanLII" in body)

    resp = c.get("/costs.csv")
    check("CSV downloads as an attachment",
          resp.headers.get("Content-Disposition", "").startswith("attachment;"))
    check("CSV opens cleanly in Excel (BOM)",
          resp.data.startswith(b"\xef\xbb\xbf"), "utf-8-sig")

    filtered = c.get(f"/costs?matter={m.id}&period=all").get_data(as_text=True)
    # The filtered-out matter still appears as a dropdown OPTION, which is
    # correct -- a lawyer must be able to switch to it. Assert on the table
    # body instead of the whole page.
    body_only = filtered.split("<tbody>")[-1].split("</tbody>")[0]
    check("matter filter applies to the rows", "Cresthaven" not in body_only)
    check("but the filtered-out matter stays selectable",
          "Cresthaven" in filtered.split("<tbody>")[0])

    print("\n9. ONE REAL CALL: usage.cost actually comes back")
    if os.environ.get("OPENROUTER_API_KEY", "").startswith("sk-or"):
        client = llm.LLMClient(model="mistralai/mistral-nemo")
        acc = llm.start_cost_run(task="probe", matter_id=None,
                                 model="mistralai/mistral-nemo")
        client.complete([{"role": "user", "content": "Reply with: ok"}])
        llm.end_cost_run()
        check("OpenRouter reported a real cost", acc.cost_usd > 0,
              f"${acc.cost_usd:.8f} for {acc.input_tokens} in / "
              f"{acc.output_tokens} out")
        check("not flagged unavailable", acc.cost_unavailable is False)
    else:
        print("  [skip] no live OPENROUTER_API_KEY")

    print(f"\n{'PASS' if not failed else 'FAIL'}: {len(passed)} passed, {len(failed)} failed")
    for f in failed:
        print(f"  failed: {f}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
