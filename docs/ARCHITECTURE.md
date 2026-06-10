# Architecture Decisions

This file records significant architectural choices made during the build, with brief rationale. Each entry should be added at the time the decision is made — not retrofitted later.

The point of this log is to make later "why did we do it this way?" questions answerable without re-reading the entire codebase. It is also reviewed by the user before each phase ends.

## Format

```
## YYYY-MM-DD: <decision title>
**Context:** what was being decided and why it came up
**Decision:** what was chosen
**Alternatives considered:** what else was on the table
**Consequences:** what this commits us to or rules out
```

## Decisions

## 2026-06-09: Day-3 — task templates are YAML, one file per task, in `prompts/templates/`
**Context:** SoW §3.10 / §5.5 require the task system prompts to be versionable files (one per task), readable by the user, and diffable by the Phase 3 prompt curator. Day 1/2 had a single hardcoded `DAY1_SYSTEM_PROMPT` in `prompts.py`.
**Decision:** Each task is one YAML file in `prompts/templates/<id>.yaml`, loaded into a validated `TaskTemplate` pydantic model on startup (cached). A file carries: `id`, `label`, `version`, `system_prompt` (task body only), `retrieval_query` (seed), `top_k`, and an `inputs` list. The loader enforces `id == filename` and rejects duplicates/empties. PyYAML is added as a dependency (also needed for `config/sources.yaml` + `required_docs.yaml` in Phase 2).
**Alternatives considered:** TOML via stdlib `tomllib` (zero new dep, but poor multi-line-prose ergonomics for quotation-heavy legal prompts); JSON (escapes multi-line prompts into one unreadable/undiffable line); a Python module per task (mixes executable code with data, weakens the versioning/curator story). YAML block scalars diff cleanly and read well — chosen for that.
**Consequences:** Adding a task = adding a YAML file (the UI and prompt assembly are generic). Templates live at repo root, outside the `src/` package; the loader resolves `parents[2]/prompts/templates` with a `MATTER_CLERK_PROMPTS_DIR` override. Not shipped as package data — acceptable for a repo-run dev tool.

## 2026-06-09: Day-3 — §1.4 safety clause is code-owned and prepended at runtime
**Context:** SoW §1.4.1 requires a *"non-removable system-prompt clause"* forbidding fabricated authority. If that clause lived inside each task's YAML, a template author — or the Phase 3 curator proposing a diff — could weaken or delete it.
**Decision:** The safety/citation-discipline text is the `SAFETY_PREAMBLE` constant in `prompts.py`. `build_system_prompt(template)` returns `SAFETY_PREAMBLE + "\n\n" + template.system_prompt`. Templates carry only the task-specific body. The preamble also asserts matter-only mode (no external authority, no legal tests/elements from memory), satisfying §1.4.1/§1.4.2 on the prompt side for every task uniformly.
**Alternatives considered:** Duplicating the clause into each YAML (drift + deletable); a separate `_safety.yaml` partial (still file-editable, still deletable). Code ownership is what makes "non-removable" literally true.
**Consequences:** Citation discipline is identical across all six tasks and cannot be edited away by template work. Draft Memo / Correspondence additionally carry an in-body matter-only refusal instruction (flag the gap rather than supply authority from memory) layered on top of the preamble.

## 2026-06-09: Day-3 — free-form Q&A becomes the Find Facts task; `task`+`structured_inputs` threaded through the pipeline
**Context:** Day 1/2 exposed a single free-form question box. Day 3 introduces named tasks; we did not want to keep a separate "free-form" mode alongside them.
**Decision:** The free-form path *is* the `find_facts` task (its YAML body is the old Day-1 prompt: answer directly, refuse when unsupported). `find_facts` is the default-selected task, so existing behaviour is preserved, not removed. `pipeline.run_query` now takes `task: str` + `structured_inputs: dict` instead of `question: str`; `top_k` became `int | None` (None = use the template's per-task default). CLI gains `--task` (default `find_facts`) plus `--question`/`--recipient`/`--categories`; `PipelineResult` gains `task`.
**Alternatives considered:** Keep a distinct free-form mode plus tasks (two code paths for the same thing); a flag enum instead of a string task id (the id maps 1:1 to a YAML filename — a string is the natural key).
**Consequences:** One uniform path for all six tasks. Required-input validation is centralized in `prompts.missing_required_inputs` and called by both CLI and web before any expensive work.

## 2026-06-09: Day-3 — generic `inputs` descriptor drives both the form and prompt assembly
**Context:** Tasks need different inputs (Find Facts/Memo: a question; Correspondence: recipient + body; Find Entities: a category multiselect; Summarize/Timeline: an optional focus or nothing).
**Decision:** Each template declares an `inputs` list of `InputField`s (`name`, `type` ∈ {text, textarea, multiselect}, `required`, `label`, `placeholder`, `options`, `default`). The web form renders controls from this list (all tasks' fields are rendered, then JS shows the selected task's group and disables the rest so hidden fields neither submit nor trip HTML5 `required`). `build_user_message` folds the same inputs into the `REQUEST:` block by label. This is slightly more than the six tasks strictly need, but it is the actual shape of the remaining SoW tasks (Draft Pleading's pleading-type/party-role selects, Phase-2 output-mode selector) — so it is the right investment, not over-engineering.
**Alternatives considered:** Special-casing each task in the template + handler (doesn't scale to 10 tasks); a separate GET round-trip on dropdown change to re-render fields (loses the chosen file).
**Consequences:** Adding a task with new input types may require a new render branch in `index.html`, but no handler/pipeline changes.

## 2026-06-09: Day-3 — query-less tasks retrieve via a per-template seed query
**Context:** Summarize / Timeline / Find Entities may run with no user question, but semantic retrieval needs a query vector.
**Decision:** Each template carries a `retrieval_query` seed describing what that task generally needs to surface (e.g. Summarize seeds on "parties, dispute, key documents, principal facts, procedural posture"). `build_retrieval_query` concatenates the seed with any user-supplied input text and never returns empty. `top_k` is tuned per task (entities/timeline retrieve wider) and is overridable from the Advanced box / `--top-k`.
**Alternatives considered:** Whole-document stuffing (not viable at the SoW's 50–200-page matter size); a fixed global query (ignores what each task actually needs).
**Consequences:** Citations remain grounded in retrieved passages for every task. Citation extraction still lists every retrieved chunk (not yet parsed from the model's actual inline cites) — unchanged from Day 1/2; tightening it is backlogged.

## 2026-06-09: Day-3 — Compare Clauses deferred to Day 4; Draft Pleading to Day 3.5
**Context:** Day 3 ships six of the ten SoW tasks. Compare Clauses is defined as a cross-document comparison (§3 table), but Day 3 is still single-PDF (the matter concept is Day 4). Draft Pleading needs the DRAFT-watermark + non-removable cover-note machinery (§1.4.3, §4.6).
**Decision:** Compare Clauses is deferred whole to Day 4 rather than shipped as a within-document workaround — a single-PDF version would mistrain the user to think of it as within-document when it is meant to be across-document. Draft Pleading is deferred to a dedicated Day 3.5 for its watermark/cover-note workflow.
**Alternatives considered:** Within-document Compare Clauses now (rejected: wrong mental model); folding Pleading into Day 3 (rejected: §1.4.3 machinery warrants its own slice).
**Consequences:** Day 3 covers Summarize, Timeline, Find Facts, Find Entities, Draft Memo, Draft Correspondence. The two deferrals are tracked for Day 4 and Day 3.5 respectively.

## 2026-05-28: OpenAI SDK as the OpenRouter transport for LLMClient
**Context:** CLAUDE.md requires that the LLM provider be swappable with a one-file change. OpenRouter is the default provider per the SoW (MiMo Pro), but the user may swap to Claude or another model for testing.
**Decision:** `LLMClient` in `src/matter_clerk/llm.py` uses the `openai` Python SDK pointed at OpenRouter's base URL (`https://openrouter.ai/api/v1`). OpenRouter speaks the OpenAI chat-completions protocol, so this is the smallest viable abstraction.
**Alternatives considered:** A hand-rolled `requests`-based HTTP client (more lines, no real benefit); LangChain or LiteLLM (too much surface area for one method); separate adapters per provider (premature).
**Consequences:** Swapping to Anthropic / OpenAI / Google direct means changing only `llm.py`. All calling code uses `LLMClient.complete(messages) -> str` and is provider-agnostic.

## 2026-05-28: pypdf for Day-1 text extraction; OCR deferred to ING-2
**Context:** Phase 1 Day 1 needs page-numbered text from a single native PDF. The SoW ingestion pipeline (§4.1) also calls for OCR via pytesseract + pdf2image for scanned PDFs, but that work is acceptance-tested under ING-2 and not required today.
**Decision:** Use `pypdf` for native-text extraction. Pages with no extractable text (empty string after strip) are not silently dropped: their page numbers are collected into a `skipped` list and surfaced to the user as a warning so the gap is visible.
**Alternatives considered:** `pdfplumber` (heavier; better at tables, no benefit for Q&A retrieval today); `pymupdf` (better extraction quality but AGPL — licence considerations we don't want to take on yet).
**Consequences:** Scanned-only PDFs produce a "no extractable text" error today. Mixed-content PDFs work but log skipped scanned pages. When ING-2 work begins, OCR slots into `extract_pdf_pages` and the `skipped` list shrinks to truly unreadable pages.

## 2026-05-28: Local embeddings via sentence-transformers (BAAI/bge-small-en-v1.5)
**Context:** Need embeddings for chunk retrieval. Two axes: provider-hosted (OpenAI / Voyage / Cohere) vs. local; and quality vs. cost.
**Decision:** Run `BAAI/bge-small-en-v1.5` locally via `sentence-transformers`. 384-dim, CPU-friendly, MIT-licensed, strong on legal-prose retrieval at this size.
**Alternatives considered:** OpenAI `text-embedding-3-small` (would send matter text to a third party for embedding too, doubling the data-egress surface); larger BGE variants (slower with limited Day-1 benefit).
**Consequences:** Matter text is not transmitted off-machine for embedding; the only egress is the final retrieved chunks passed to the LLM (which the SoW already accepts as the privacy boundary). Cold-start cost: the model downloads ~130 MB on first use.

## 2026-05-28: One Qdrant collection per PDF, keyed by content hash
**Context:** Day-1 scope is single-PDF. We don't yet have the matter concept, but we want re-runs of the same PDF to skip re-ingestion.
**Decision:** Default collection name is `day1-<first 16 hex of SHA-256(pdf)>`. `--collection` overrides; `--reindex` forces a fresh ingest.
**Alternatives considered:** Single shared collection with per-document filters (premature — the matter concept will reorganise this in Phase 1 later anyway); name from filename (collides on identical names with different contents).
**Consequences:** This scheme is explicitly Day-1 only. When the matter concept lands later in Phase 1, one collection per matter (with document-level filter) replaces this; the `day1-*` collections become disposable.

## 2026-06-05: Day-2.5 — OCR fallback pulled forward from Phase 2
**Context:** The user's real-world PDFs are predominantly scanned, not native-text. Without OCR the Day-1 / Day-2 pipeline silently skipped any page pypdf couldn't read, which is unacceptable for the actual document mix.
**Decision:** OCR is now an inline fallback inside `extract_pdf_pages` (`ingest.py`). For each page: try pypdf first; if it returns empty after `strip()`, render the page at 300 DPI via `pdf2image` and OCR it with `pytesseract`. Per-page citations are preserved — OCR'd page 7 still cites as `p.7`. OCR is always-on for pypdf-empty pages (no `--ocr` flag); the user shouldn't have to know in advance whether their PDF is scanned, and the cost of OCR'ing a truly-blank page is a few seconds.

`extract_pdf_pages` now returns three lists: pages-with-text (native + OCR'd, unified — the citation layer doesn't and shouldn't care about the source), `ocr_pages` (which page numbers came from OCR), and `unreadable_pages` (pypdf empty, OCR empty, image is not effectively blank). Truly-blank pages — detected when ≥ 99% of pixels in the rendered grayscale image are ≥ 240/255 — are silently dropped on the theory that "this page has no content" is the page's nature, not a warning. `PipelineResult` gains `ocr_pages` and renames `skipped_pages` → `unreadable_pages`; CLI and web both surface both lists (blue info banner / yellow warn banner in the browser; INFO / WARN log lines in the terminal).

**System dependencies — required on PATH for the pip packages to work:** Tesseract OCR (verify with `tesseract --version`) and Poppler (verify with `pdftoppm -v`). The Python wrappers `pytesseract` and `pdf2image` shell out to these binaries; `pip install` alone is not sufficient.

**Alternatives considered:** Opt-in `--ocr` flag (rejected: real-world PDF mix means most invocations want OCR on, and users shouldn't need foreknowledge of their PDFs); always-OCR with no pypdf fast path (wastes time on native PDFs); Tesseract confidence via `image_to_data` for the blank-vs-unreadable split (rejected: the empty string from `image_to_string` is already an unambiguous signal — the pixel-brightness histogram is simpler and engine-independent); parallel OCR across pages via `multiprocessing` (deferred until ingest latency becomes a real problem; adds non-trivial complexity to error propagation and result ordering).
**Consequences:** First ingestion of a scanned PDF is slow — roughly 1–3 seconds per page at 300 DPI on typical CPUs, so a 50-page scan is 1–2 minutes. Subsequent runs on the same content hash hit the cached Qdrant collection; no re-OCR. Per-page OCR is wrapped in `try / except` (30 s timeout, render failure) so a single bad page degrades to "unreadable" rather than killing the whole ingest. The web UI has no per-page progress yet — the form-post just hangs until the full ingest completes. The SoW ING-2 acceptance test (OCR of a scanned PDF) is satisfied by this change; Phase 2's scope shrinks correspondingly.

## 2026-06-04: Pipeline orchestration extracted into `pipeline.py`
**Context:** Day 1 inlined ingest → retrieve → answer inside `cli.py`. Day 2 needs the same flow driven from a Flask request handler. Duplicating the orchestration would mean the CLI and the web UI could drift, and any later change (Phase 2 CanLII augmentation, Phase 3 feedback hook) would need to be made twice.
**Decision:** Move the orchestration to `pipeline.run_query(pdf_path, source_name, question, top_k, reindex, collection) -> PipelineResult` (pydantic). `cli.py` and `web.py` are now thin formatters around the same call. Pipeline failures are surfaced via `QdrantUnreachable` and `PdfHasNoText` exceptions so each caller can render them appropriately.
**Alternatives considered:** Keep `cli.py` authoritative and have the web handler shell out to it (slow and string-marshalled); duplicate the code (predictable drift).
**Consequences:** `source_name` is now separate from `pdf_path` — required because the web handler hands the pipeline a tempfile path but wants citations to read the original upload's filename. CLI behaviour is unchanged.

## 2026-06-04: Flask + synchronous form-post for the local web UI
**Context:** Day 2 wraps the existing pipeline in a browser-accessible UI. Single-user, localhost-only, no auth, no concurrent load.
**Decision:** Flask 3 with two routes (`GET /`, `POST /ask`), Jinja templates inside the package (`src/matter_clerk/templates/`), and inline CSS in `base.html`. The dev server is started via `werkzeug.serving.make_server` directly (not `app.run`) so the startup banner is ours and the Werkzeug access log is suppressed. Port defaults to 5050 (overridable via `MATTER_CLERK_PORT`) — avoids the common 5000/5001 collisions.
**Alternatives considered:** FastAPI + Starlette (richer than needed for two routes; Jinja support is less native); async + polling progress (no UX benefit when the LLM call dominates total latency); HTMX (adds a dep for one form-post).
**Consequences:** Server is single-process. Re-reading file changes requires restarting `python -m matter_clerk.web` (no auto-reload, by design — predictable shutdown semantics matter more than dev convenience here).

## 2026-06-04: In-flight requests run to completion on Ctrl+C (`daemon_threads = False`)
**Context:** Werkzeug's `ThreadedWSGIServer` sets `daemon_threads = True` by default. Under that default, SIGINT during a request kills the worker thread without running `finally` clauses — which would leak the upload tmp file we create in `web.ask`.
**Decision:** Set `server.daemon_threads = False` after constructing the server. Ctrl+C closes the listening socket immediately and the process waits for in-flight requests to finish before exiting; their `try/finally: tmp_path.unlink(missing_ok=True)` runs.
**Alternatives considered:** Register `atexit` cleanup that scans for orphaned tmp files (fragile, cross-platform-ugly); switch to `threaded=False` (would block the index page while `/ask` is processing — minor but real UX regression).
**Consequences:** "Press Ctrl+C to stop" may pause for a few seconds if the LLM call is mid-flight when the signal arrives. A second Ctrl+C still escapes. No tmp-file leakage in either path.

## 2026-06-04: Markdown rendered via `markdown` + sanitised through `bleach`
**Context:** The LLM emits markdown. The web result page must render that — including tables, for the timeline output coming in Day 3. A localhost-only tool is still a self-XSS surface if the model returns `<script>` or `<img onerror=…>`.
**Decision:** Pipeline → `markdown.Markdown(extensions=["tables", "fenced_code"])` → `bleach.clean` with a prose+table allowlist (`p, br, hr, em, strong, code, pre, ul, ol, li, blockquote, h1–h6, table, thead, tbody, tr, th, td`). No `<a>`, no `<img>`, no `<script>`, no inline `style`, no attributes at all.
**Alternatives considered:** `markdown-it-py` (also good; `markdown` is older but smaller and the table extension is solid); skip sanitisation given localhost-only (small risk × zero cost to mitigate = mitigate).
**Consequences:** The bracket-citation strings `[filename.pdf p.N]` render as inline text (correct — they're not links yet). When Phase 2 introduces verified CanLII URLs, this entry should be revisited to widen the allowlist to include `<a href>` with a URL-scheme check.

## 2026-05-28: Per-page token-window chunking (no cross-page chunks)
**Context:** SoW §4.1 specifies 600–800-token chunks with ~100-token overlap, each tagged with source filename and page number. The natural way to honour the "tagged with page number" requirement is to never let a chunk straddle a page boundary.
**Decision:** Chunk within each page independently using `tiktoken` cl100k_base for token counting; chunk size 700, overlap 100. Every chunk maps to exactly one page citation.
**Alternatives considered:** Cross-page chunks with a page-range citation (introduces a "which page does this quote come from" ambiguity that the citation discipline can't tolerate); cross-page chunks pinned to the first page (a quiet lie).
**Consequences:** Very short pages produce small chunks. Retrieval quality is unaffected. Citation honesty is preserved.
