# Backlog

Items deferred from earlier phases, ranked by when a decision needs to be made.

The point of this file is to record decisions we *consciously* punted on, so they aren't lost to memory. When we hit the right stage of the project, the relevant items move from this file into active work.

## Format

```
## <Title>
**Deferred from:** <phase or date>
**Trigger to revisit:** <what event in the project should prompt this>
**Context:** <why we deferred and what's at stake>
**Options under consideration:** <if applicable>
```

---

## OCR improvements: layout-aware extraction (tables, forms, columns, sidebars)
**Deferred from:** Phase 1 Day 2.5 (June 5, 2026)
**Trigger to revisit:** After 2–3 weeks of real-world tool use, once the user has a representative sample of documents that have failed OCR in practice — OR before Phase 2 CanLII verification work begins, whichever comes first.

**Context:** Tesseract is great at prose but reads pages as a 1D stream. Tables, forms, columns, sidebars, footnotes, and checkboxes all flatten into linear text that loses structure. In Canadian legal practice this affects condominium financial schedules, court forms, organizational charts, due-diligence rent rolls, and many exhibit types. We accepted current Tesseract behaviour for Day 2.5 because (a) it's adequate for prose-heavy documents, (b) we don't yet know the user's actual document mix across many matters, and (c) jumping to a more sophisticated solution requires understanding the trade-offs across multiple options.

**Options under consideration:**

- **Option A — Local layout-aware open source.** Pair Tesseract with LayoutParser, PaddleOCR, or IBM Docling. Adds 1–2 GB of model downloads; works locally; no per-page cost. Inconsistent on complex real-world tables. ~1–2 days of engineering. Partial improvement.

- **Option B — Commercial document-AI APIs.** AWS Textract (~$0.015/page for tables+forms), Google Document AI, or Azure Document Intelligence. Sends pages to a cloud service. Strong table and form extraction. Industry standard for legal-tech. ~$1.50 per 100-page matter. Substantial improvement on structured documents.

- **Option C — Multimodal LLM for OCR.** Send rendered page images to Claude Opus, GPT-5, or Gemini 2.5 Pro. Best at unusual layouts (court stamps, handwritten endorsements, mixed merged-cell tables). ~$0.01–0.03/page. Same confidentiality boundary as Option B. Higher cost than Textract, often more useful output.

**Decision criteria when revisited:**
- Does the user's actual document mix justify the per-page cost?
- Which document types fail OCR most often in real use?
- Is per-page confidentiality treatment of B/C acceptable, or is local-only Option A preferred?

---

## OCR improvements: smaller targeted enhancements
**Deferred from:** Phase 1 Day 2.5 (June 5, 2026)
**Trigger to revisit:** Whenever the user has a couple of free hours and a willingness to do polish work. Independent of the bigger Options A/B/C decision above.

**Context:** Several small OCR refinements would each take a few hours and would each add real value, independently of the bigger layout-aware decision.

- **Citation markers for OCR'd content.** Mark citations that came from OCR'd text — e.g., `[document.pdf p.3 (OCR)]` — so the user knows the quoted snippet may not literally match the source page. Important before Phase 2 CanLII verification.
- **Image preprocessing.** Add basic OpenCV preprocessing (deskew, denoise, contrast normalization) before Tesseract. ~5–15% accuracy improvement on average scans. New dependency: `opencv-python` or similar.
- **French language support.** Install Tesseract French language data and try both English and French extraction. Essential for federal and Quebec work. Trivial install change.
- **Per-page OCR confidence reporting.** Surface Tesseract's confidence scores via `image_to_data` and display per-page confidence in the UI. Helps user know when to be skeptical of an answer.
- **Auto-rotation correction.** Detect and correct rotated or upside-down pages before OCR. Affects scans of folded documents or photographed evidence.
- **Header/footer/page-number stripping.** Detect and suppress repeating page headers, footers, and page numbers so they don't dilute retrieval as if they were content.

---

## Authority Library (user-uploaded case law, articles, treatises)
**Deferred from:** Conversation on May 28, 2026 (before Phase 1 build started)
**Trigger to revisit:** After CanLII retrieval is working in Phase 2, before final Phase 2 sign-off.

**Context:** The user proposed a user-curated authority library alongside CanLII — case PDFs the user wants to retain, articles, treatises, internal precedent memos — searchable with the same court hierarchy ranking applied to CanLII results. We agreed this is a meaningful improvement but deferred so CanLII retrieval discipline could be established first without the additional complexity of a second authority source.

**Scope when revisited:**
- A separate "Authority Vault" module alongside Matter Vaults.
- Each upload classified (auto-detected, user-confirmed) into one of: SCC / ONCA / ONSC+Divisional / Other Canadian Courts / Secondary Sources / Statutes.
- Joint ranking with CanLII results applying the SoW Section 4.3.1 court hierarchy.
- Secondary sources marked "commentary, not binding authority" with different citation label.
- Items marked "user-attested" vs "CanLII-verified" so chain of trust is visible in outputs.

---

## Phase 2 acceptance testing
**Deferred from:** SoW v3 (May 27, 2026)
**Trigger to revisit:** Before Phase 2 work begins.

**Context:** The user chose to defer formalising the acceptance test suite during the SoW v3 revision. The categories and test design exist in SoW Section 5 but were not extended to cover the v3 additions (10th pleadings task, the four-rule-source procedure tracker, the court-hierarchy ranking, the safety guards). Before Phase 2 ships, the test plan needs to be brought up to date.

**Specifically required:**
- A concrete acceptance run-book — "use this PDF, run this command, expect this output" — alongside the automated test harness.
- Test fixtures: a documented test matter (50–200 pages) and a documented adversarial matter (≥30 pages) with the specific failure modes the SoW lists.
- Gold-set queries — 10–20 with their approved outputs.

---

## Performance: parallel OCR across pages
**Deferred from:** Phase 1 Day 2.5 (June 5, 2026)
**Trigger to revisit:** If/when scanned-PDF ingest times become painful on real matters (50+ pages, several minutes per ingest).

**Context:** Current OCR runs one page at a time. Parallel OCR via `multiprocessing.Pool` would roughly N-core the throughput on scanned PDFs but adds non-trivial complexity (worker-pool lifecycle, error propagation, ordering of results). Worth it only if a real workflow demands it.

---

---

## Find Entities: split branded products and manufacturers from legal-party Organizations
**Deferred from:** Phase 1 Day 3 (June 2026)
**Trigger to revisit:** Either (a) when the Phase 3 prompt curator system is built and can process this kind of refinement structurally, or (b) sooner if the user encounters this issue across multiple matters and wants a quick template update.

**Context:** During Phase 1 Day 3 testing on the Condo Bylaw 6 matter, the Find Entities Organizations list mixed legal-party organizations (Toronto Standard Condominium Corporation No. 2565, Shibley Righton) with product manufacturers specified in the bylaw's standard-unit finishes (Schneider Electric, Johnson Control, Honeywell, Benjamin Moore). Both categories are real organizations, but their legal relevance is very different.

In most legal work, product brand names mentioned in source documents are descriptive noise — a contract drafted on a Dell laptop, appliances in a matrimonial home, paint specifications in a fit-out spec. They're not parties, not witnesses' employers, and not material to the cause of action. Mixing them with legal-party organizations in a single list creates noise the user has to mentally filter.

However, brand specifications *are* legally material in certain matter types:
- Condominium standard-unit definitions (allocates repair/insurance responsibility — current matter)
- Product liability claims (manufacturer is the defendant)
- Contract disputes where the brand is the specified deliverable
- Construction defect litigation (manufacturers as third-party defendants)
- Insurance subrogation against component manufacturers
- Trademark and IP matters

So the categorization should be user-controllable rather than hardcoded either way.

**Recommended approach:** Add a sixth user-selectable category to the Find Entities template ("Branded products and manufacturers") defaulted to OFF. This keeps the main Organizations list clean for typical legal work while letting the user opt in for matters where brand specifications are legally material. YAML-only change; no Python code modification needed — the existing generic inputs descriptor mechanism handles user-selectable categories.

**Note:** This is the kind of nuance the prompt curator pattern (SoW Section 4.8) is designed to handle systematically by observing patterns across multiple matters before changing prompts. Documenting now so it can feed into Phase 3 curator development. Premature optimization to the template based on a single matter observation is exactly the failure mode the curator pattern is meant to prevent — so this is documented here, not patched today.