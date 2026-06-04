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

## 2026-05-28: Per-page token-window chunking (no cross-page chunks)
**Context:** SoW §4.1 specifies 600–800-token chunks with ~100-token overlap, each tagged with source filename and page number. The natural way to honour the "tagged with page number" requirement is to never let a chunk straddle a page boundary.
**Decision:** Chunk within each page independently using `tiktoken` cl100k_base for token counting; chunk size 700, overlap 100. Every chunk maps to exactly one page citation.
**Alternatives considered:** Cross-page chunks with a page-range citation (introduces a "which page does this quote come from" ambiguity that the citation discipline can't tolerate); cross-page chunks pinned to the first page (a quiet lie).
**Consequences:** Very short pages produce small chunks. Retrieval quality is unaffected. Citation honesty is preserved.
