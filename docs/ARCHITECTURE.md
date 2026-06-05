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
