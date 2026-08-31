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

## Auto-open browser sometimes fails on double-click launch
**Deferred from:** Phase 3 Session 3 verification (August 2026)
**Trigger to revisit:** Session 5 (the installer creates a Start Menu shortcut, which is a shell launch — the same context that fails, so the installer makes this the *default* path rather than an edge case). Sooner if a lawyer reports it.

**Context:** Launching `MatterClerk.exe` by double-clicking in File Explorer sometimes fails to open the browser, even though Flask starts normally and the app is reachable. Launching the same exe from PowerShell (`.\dist\MatterClerk\MatterClerk.exe`) works reliably. Not a blocker — the console window prints the URL and the user can navigate manually — but a lawyer who double-clicks a shortcut and sees a console window with no browser will read the app as broken.

**Why Session 3 verification did not catch this — worth knowing before re-testing.** The Session 3 confirmation that "the browser opens" was produced by setting the `BROWSER` environment variable to a marker script and observing that the launcher invoked it with the correct URL. That genuinely proved the readiness-poll logic and the URL construction, and both are fine. But setting `BROWSER` routes `webbrowser.open()` down the `GenericBrowser` path — a plain subprocess spawn — which is **not** the path a real launch takes. With `BROWSER` unset, Windows uses `webbrowser.WindowsDefault`, which calls `os.startfile()`. So the failing code path was never exercised. Any re-test must run without `BROWSER` set, and must launch from Explorer, not a terminal.

**Leading hypothesis: COM is not initialized on the browser thread.** `os.startfile()` wraps `ShellExecuteW`. ShellExecute delegates to shell extensions and therefore expects COM to be initialized on the calling thread; Microsoft's own documentation says to call `CoInitializeEx` before `ShellExecuteEx` for this reason. The launcher opens the browser from a `threading.Thread(target=_open, daemon=True)` (see `matter_clerk_launcher.py`), and Python does not initialize COM on worker threads. Whether that matters depends on which handler the shell picks for an `http:` URL, which in turn depends on the default browser and on the launching process's own apartment state — which is exactly the kind of dependency that would make this fail from Explorer, succeed from PowerShell, and behave intermittently. This is a hypothesis, not a diagnosis; it has not been confirmed against a failing run.

**Options under consideration:**
- **Initialize COM on the browser thread.** `ctypes.windll.ole32.CoInitializeEx(None, 2)` (apartment-threaded) at the top of `_open`, with `CoUninitialize` on the way out. Smallest change, directly tests the hypothesis, and if the hypothesis is right it is the correct fix rather than a workaround.
- **Do not open the browser from a worker thread at all.** Poll `/healthz` in the thread but hand the actual open back to the main thread before `serve_forever()`. Sidesteps the thread-context question entirely; costs a little launcher restructuring.
- **Bypass `webbrowser` and spawn the shell explicitly** — `subprocess.Popen(["cmd", "/c", "start", "", url], creationflags=CREATE_NO_WINDOW)`. Blunt but very predictable, and it is what several packaged Python apps end up doing. Loses `webbrowser`'s `BROWSER`-variable courtesy, which nobody in this deployment is using.
- **Surface the URL better and stop treating auto-open as load-bearing.** Print the URL prominently, and have the installer's shortcut point at a `.url` file or a one-line launcher that opens the browser itself. Worth doing *regardless* of which fix is chosen: the browser can fail to open for reasons entirely outside our control (no default browser registered, locked-down machine), and the console should never leave the user without a next step.

**Check while in here:** whether the failure is actually intermittent or is fully determined by launch context. "Sometimes" from a handful of double-clicks may just be an unrecognised deterministic trigger — for example first-launch-after-boot, or whether a browser process is already running. Nailing that down first will make the fix much easier to verify, since an intermittent bug that is really deterministic is easy to declare fixed by accident.

## Statutory authority in authority mode (Condominium Act, Limitations Act, etc.)
**Deferred from:** Phase 2b polish (August 28, 2026)
**Trigger to revisit:** Phase 3, or sooner if lawyer testing shows Draft Memo answers on statute-driven issues are dominated by `[AUTHORITY REQUIRED]` markers. The procedure tracker's rule-source fetching (SoW §4.5) is the natural place to build the retrieval channel this needs — revisit when that work starts, since the two share a fetcher.

**Context:** Authority mode authorizes **case citations only**. Rule 2 of `AUTHORITY_MODE_INSTRUCTION` requires a neutral citation (`2020 ONCA 471`) so the citation can be resolved against CanLII's `caseBrowse` endpoint — and no statute has one. This surfaced while measuring the reasonable-confidence recalibration (see ARCHITECTURE 2026-08-28): on a Draft Memo query about a condominium corporation's repair obligation, both MiMo Pro and Claude Opus 4.8 produced **zero** citations, and every `[AUTHORITY REQUIRED — lawyer to confirm]` marker across four runs named a *statutory* proposition — *Condominium Act, 1998* ss. 56, 89–91; *Limitations Act, 2002*; the *Insurance Act*. A control query whose governing authority is case law drew four verified citations from the same model under the same prompt, confirming the gap is the authority *type*, not the prompt or the model.

The models were behaving correctly. A real Ontario legal memo is very often statute-first with cases interpreting the statute, so this is not an edge case — it is a large fraction of the work the Draft Memo task exists to do, and today that fraction comes back as gap markers.

**This is not fixable by prompt.** The project's governing rule (CLAUDE.md; SoW §4.3) is that the system never cites authority it has not retrieved, and the whole point of Phase 2b is that a citation is only as good as the check behind it. Letting the model state statutory content from training knowledge would reintroduce exactly the hallucination risk the phase was built to close, and there is no CanLII-style existence check for "s. 89(5) says X" — the failure mode for statutes is *misquotation*, not *non-existence*, so an existence check would not even be the right instrument.

**Options under consideration:**
- **Retrieve statute text and inject it, like matter documents.** CanLII has a `legislationBrowse` API covering Ontario statutes and regulations; fetched section text goes into CONTEXT with a `[SOURCE: ...]` locator and the model cites it the same way it cites a matter document. Strongest option: it makes statutory citation *grounded* rather than *checked*, which is the stronger guarantee, and it reuses the existing citation pipeline rather than adding a parallel one. Cost: a section-level retrieval design (a memo needs ss. 89–91, not the whole Act) and cache-freshness rules, since statute text is amended.
- **A separate verification path for statutory citations** — model cites `Condominium Act, 1998, s. 89(5)`, code fetches that section and compares the model's characterisation against the real text. Weaker than injection (it checks a claim after the fact rather than grounding it) and the comparison step is itself an LLM judgment call.
- **A distinct `[STATUTE REQUIRED — lawyer to confirm: <section>]` marker**, narrower than the current generic one. Does not close the gap, but makes it legible: a lawyer skimming a memo could see at a glance that the missing authority is a specific statutory section they can look up in seconds, rather than an open-ended research task. Cheap, and worth doing regardless of which of the above is chosen.
- **Leave as-is.** Defensible only while authority mode is understood as a case-law feature. The UI now says so: the radio reads "Matter + CanLII **case** authority" with the helper line "Verifies case citations against CanLII. Statutory authority still requires manual verification." (August 28, 2026). That makes the limit honest but does not close it — a lawyer who needs s. 89 of the *Condominium Act* still gets a gap marker.

## CLI matter query subcommand
**Deferred from:** Phase 1 Day 4b (June 25, 2026)
**Trigger to revisit:** When matter work needs to be scriptable/automatable from the terminal (batch runs, CI-style regression over a matter, or a user who prefers the CLI for matters) — or whenever the next CLI pass touches `_matter_main`.

**Context:** Day-4b added cross-document scatter-gather retrieval (`pipeline.run_matter_query`), but only the web UI dispatches to it. The CLI's `matter` subcommand is management-only (`create | list | add | show`); there is no way to *run a task across a matter* from the terminal, so scatter-gather is **web-only** today. The CLI's flat query path (`matter-clerk --pdf <file> --task <task>`) remains single-file/ad-hoc. This was deliberate: the Day-4b scope was the web experience, and the CLI is drifting toward scripting-only for matters. No safety gap — the limitation gate and citation discipline live in the pipeline, so any future CLI verb inherits them for free.

**Scope when revisited:**
- A `matter-clerk matter query <name> --task <task> [--file <id>] [--top-k N] [task inputs...]` verb that resolves the matter by name, collects the matter's ingested files, and calls `run_matter_query` (or `run_query` when `--file` restricts to one) — mirroring the web dispatch.
- Render the answer + citations + the "Drew on" file list to the terminal (the web result page's provenance line has a natural CLI equivalent).
- Pleading inputs (`pleading_type`, `claim_particulars`, the limitation confirmation) need a CLI surface; the limitation refusal must print the per-file `signals_by_file` and exit non-zero until confirmed.
- Fire the same `matter_query` / `limitation_review` audit events the web path does.

**Note:** Low risk (the pipeline already does the work), but non-trivial surface area for pleading inputs and the limitation-confirmation round-trip — budget for that, not just the happy path.

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