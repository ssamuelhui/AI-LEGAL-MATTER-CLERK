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

## 2026-08-30: Phase 3 — vector store moved from Qdrant to ChromaDB (embedded)
**Context:** Phase 3 is deployment: the tool ships to Ontario lawyers as a Windows installer. Qdrant runs as a Docker container, and Docker is the single largest obstacle to that — corporate laptops frequently prohibit Docker Desktop outright, its updates break working installations, and it is ~500MB of infrastructure a lawyer has to understand and keep running before the tool will answer a question. ChromaDB has an embedded mode: an in-process store over a local directory, no daemon, no ports, nothing to start. This is the largest single architectural change since Day 4a and unblocks the rest of Phase 3.
**Decision:** `vectorstore.py` rewritten against `chromadb.PersistentClient`; `qdrant-client` removed from `pyproject.toml`; `docker-compose.yml` moved to `docs/legacy/` for reference. Store lives at `data/chroma/` (gitignored — it holds matter text), overridable with `CHROMA_DB_PATH`. `QDRANT_HOST`/`QDRANT_PORT` are gone and no variable replaces them.

- **`ScoredChunk.score` remains a cosine SIMILARITY, higher-is-better, and this is the load-bearing decision in the port.** Qdrant's `Distance.COSINE` returns a similarity; Chroma returns a cosine *distance*, where lower is better. Porting `search_across_collections`'s `sort(reverse=True)` literally would have inverted the ranking of every retrieval in the tool — and inverted it **silently**: chunks are still returned, no exception is raised, the memo still cites real passages from real documents. They would simply be the *least* relevant passages in the matter. `_to_similarity()` converts at the module boundary so every caller, sort, and threshold above it keeps the meaning it already had. Verified empirically against Chroma 1.5.9 (identical vector → distance 0.0, orthogonal → 1.0) rather than assumed, and asserted directly in `verify_vectorstore.verify_ranking_direction`.
- **Cosine is configured explicitly** via `configuration={"hnsw": {"space": "cosine"}}` — Chroma's default space is **L2**. Because `embed()` normalises vectors, L2 and cosine happen to rank identically today, which means a wrong space would NOT have shown up in any ranking test. It is asserted directly on the collection config instead.
- **Bring-your-own embeddings, with `embedding_function=None` on every collection access.** Left unset, Chroma attaches its `DefaultEmbeddingFunction`, which downloads an ONNX all-MiniLM-L6-v2 model on first use. For an offline Windows installer that is both a surprise network fetch during a lawyer's first query and a second, entirely unused embedding model in the bundle. Confirmed accepted in 1.5.9 before relying on it.
- **`search()` now returns `ScoredChunk` instead of raw Qdrant points.** It was the one function leaking a vendor type: `pipeline.run_query` reached into `h.payload["source"]`. Its two siblings already returned `ScoredChunk`, so this makes the module's surface uniform and means no store-specific object survives above `vectorstore.py` — which is what makes the *next* store swap genuinely a one-file change, rather than nominally one.
- **Chunk ids are `sha256(content_sha256 + chunk_index + text[:200])`**, replacing Qdrant's positional integers. Deterministic from content, so re-ingesting identical bytes rewrites the same ids rather than accumulating duplicates, and an id is reproducible across machines.
- **Page number and OCR status are NOT stored as separate metadata columns.** Both are already encoded in `locator` ("p.5 (OCR)"), which is the string echoed verbatim into `[SOURCE: ...]`, into the model's citation, and into the final `Citation`. Splitting them out would create a second source of truth for what a citation says, require this layer to parse a citation format it should not know about, and break on emails, whose locators carry no page at all. `locator` is stored and returned untouched.
- **Health checks had to be retargeted, not merely reworded.** Chroma's client exposes `list_collections()` and has no `get_collections()`, so `_precheck_qdrant` and `_qdrant_ok` would have raised `AttributeError` on every request and reported a perfectly healthy store as a dead database. They became `_precheck_store` / `store_ok`, and the three templates that told lawyers to run `docker compose up -d` now name a filesystem problem a lawyer can actually act on.
**Alternatives considered:** Keeping `qdrant-client` installed alongside Chroma for rollback (rejected with the user — it inflates the installer, confuses maintainers about which store is live, and does not actually enable rollback, since the real path is `git revert`); writing a Qdrant→Chroma migration tool (rejected — no cross-vendor tool exists that is worth the engineering at our matter sizes, and re-ingestion is a few minutes per matter); letting Chroma compute embeddings via an embedding function (rejected — see the ONNX download above, and it would couple retrieval quality to a component we do not control); storing `page`/`was_ocr` as first-class metadata as originally sketched (rejected — see the locator reasoning above).
**Consequences:** **Existing installations must re-ingest their matter files** — vectors do not transfer. `matter_clerk.db`, `data/matters/`, and the audit log are untouched, so matters, filenames, and history survive; only the searchable index is rebuilt. `MIGRATION.md` documents the steps. `qdrant_storage/` is deliberately left on disk and untouched by the new code, so `git revert` restores a working Phase-2b tool with its index **already populated**.

A real capability is given up: Qdrant was a server, so the web app and the CLI could use it concurrently. An embedded store is owned by one process — multiple threads are fine (the Flask server is threaded and Chroma serialises internally), but running the CLI while the web app is up is not supported. Acceptable for the single-lawyer-single-laptop target, and documented rather than left to be discovered.

Chunking, embeddings, retrieval semantics, collection naming, citation locators, the limitation gate, DRAFT machinery, and Phase 2b citation verification are all unchanged. `tests/acceptance/verify_vectorstore.py` (48 checks) covers payload round-trip, ranking direction, scatter-gather merge, per-file grouping totality, `all_chunks` pagination, collection lifecycle, id derivation, metadata, and the cosine configuration — all against a real temp-directory store with no Docker and no network. The three existing suites pass unchanged (74 / 38 / 148); `verify_compare_clauses` needed only its stub reshaped, since `connect` lost its `(host, port)` arguments.

## 2026-08-28: Authority-mode radio scoped to "case authority", and its option strings made a checked invariant
**Context:** The authority-mode radio read "Matter + CanLII authority", which does not tell a lawyer that only *case* citations are verified. The gap it papers over is real and was measured the same day (see the entry below): a memo on a statute-driven issue comes back as `[AUTHORITY REQUIRED]` markers, and nothing in the UI explains why. Relabelling it surfaced a second problem: the radio renders `value="{{ opt }}"` and displays the same string, so **the option text IS the value the form submits**, and `authority_mode_enabled` gates on `== AUTHORITY_MODE_ON`. Editing the label in the template alone would have left the form submitting a string no longer equal to the constant — authority mode would have stopped enabling, silently, with no error anywhere.
**Decision:**
- **Label is now "Matter + CanLII case authority"**, with an always-visible helper line under the radio set: *"Verifies case citations against CanLII. Statutory authority still requires manual verification."* It is deliberately **not** folded into the existing `.authority-hint` banner, which JS reveals only once authority mode is selected — this line states what the mode *covers*, so it has to be readable while the lawyer is still choosing. The banner keeps its distinct job (what verification does and does not prove, read before tokens are spent) and still matches on `value.indexOf("CanLII")`. `result.html`'s mode heading was updated in step, so the form and the result page cannot describe the same mode differently.
- **`check_authority_anchors` now also asserts that every AUTHORITY_MODE_TASKS template declares an `authority_mode` input whose `options` equal `AUTHORITY_MODE_OPTIONS` exactly**, raising at template-load time otherwise. This extends the function's existing remit — it already guards prompt-anchor drift — to the second, quieter member of the same failure class. A missing input raises too: a task in `AUTHORITY_MODE_TASKS` with no radio cannot reach authority mode from the form at all.
**Alternatives considered:** Splitting the radio's display label from its submitted value so the label could change freely (rejected — it adds a value/label indirection to `InputField` for one field, and the single-string form is what makes the drift *detectable* by an equality check); leaving the YAML/constant agreement to the comment that already asserted it (rejected — a comment is not an invariant, and this change was itself a live instance of the bug it warns about); putting the helper text in the banner (rejected — it would only appear after the choice it is meant to inform).
**Consequences:** The option string is now genuinely code-owned: it lives in `AUTHORITY_MODE_OPTIONS` and both YAML files, and the three cannot drift apart without a loud failure at startup rather than a quiet fallback to matter-only mid-matter. Verified by mutation — a one-character change, a stale pre-polish label, and a removed input each raise; the clean load does not. Whoever renames this option next must change all three places, which is the intended cost. All four acceptance suites pass unchanged (74 / 38 / all / 148); no prompt text, gating logic, or verification behaviour was touched.

## 2026-08-28: Phase 2b polish — authority-mode prompt recalibrated to reasonable confidence
**Context:** Authority mode was producing zero case citations from both MiMo Pro and Claude on the Draft Memo task, making it functionally identical to matter-only mode. The suspected cause was `AUTHORITY_MODE_INSTRUCTION` rule 3, which demanded certainty: *"If you are not certain a case is real, DO NOT CITE IT."* Since no rule anywhere in the block **requires** the model to cite anything, "cite nothing" satisfied every rule perfectly — a globally safe strategy that both models found and sat in.
**Decision:** Rebuilt `AUTHORITY_MODE_INSTRUCTION` around **calibration rather than added strictness**, keeping the rule numbering stable.
- **Rule 3 now sets the threshold at reasonable confidence, not certainty** — "Cite when you have reasonable confidence a case exists. You do not need certainty. Verification will catch factually incorrect citations." Declining to cite is reserved for having *no reasonable basis*.
- **An explicit positive expectation was added above rule 1**, because softening rule 3 alone does not remove the "cite nothing" equilibrium — nothing in the old block ever asked for a citation. The block now states that a document produced in this mode is expected to carry authority and that citing nothing "is not the cautious answer, it is an incomplete one."
- **Rule 7 was re-framed from a warning into the justification for rule 3.** It previously ended "This is not a reason to cite defensively or to hedge — it is a reason to cite only what is real", which reinforced suppression. It now explains that the downstream CanLII check is *why* reasonable confidence suffices — the model is not the last line of defence and should not behave as though it is — while still refusing to license guessing.
- **Rules 1 and 6 are NOT softened, and rule 6 now says so explicitly.** Verification establishes that a case *exists*; nothing anywhere checks that it *held* what the sentence claims. That asymmetry is the reason rule 3 can be relaxed and rule 6 cannot, so the text states the distinction rather than leaving the model to infer it.
- **Rule 4's escape hatch is unchanged in substance** but is now positioned against rule 3 — "not a substitute for a citation you could reasonably give" — so it stays available without becoming the default.
**Alternatives considered:** Leaving rule 3 and adding a citation quota (rejected — a quota manufactures pressure to cite *something*, which is precisely how fabrication starts); deleting rule 3 entirely (rejected — it is the rule that names the no-reasonable-basis case and routes it to rule 4); softening rule 6 alongside rule 3 for consistency (rejected — nothing downstream verifies a holding, so the two rules must be calibrated differently and the text now says why).
**Consequences — measured, and the headline finding is not the one the change was aimed at.** A four-arm A/B (old vs new instruction × MiMo Pro vs Claude Opus 4.8) on the same Draft Memo query against the Imperial Plaza matter, plus a two-arm control on a deliberately case-law-shaped query:

| Query | Model | Old prompt | New prompt |
|---|---|---|---|
| Heat-pump repair obligation | MiMo Pro | 0 citations | **1 verified** (*F.H. v. McDougall*, 2008 SCC 53) |
| Heat-pump repair obligation | Claude Opus 4.8 | 0 citations | 0 citations |
| Oppression remedy (control) | Claude Opus 4.8 | 4 verified | 4 verified |

The recalibration is a real but marginal gain on the original query (MiMo 0 → 1). **The zero-citation behaviour was mostly driven by the query, not the prompt.** Authority mode authorizes *case* citations only — rule 2 requires a neutral citation, which no statute has — and the heat-pump memo's authority is almost entirely statutory (*Condominium Act, 1998* ss. 56, 89–91; *Limitations Act, 2002*). Every `[AUTHORITY REQUIRED]` marker in all four runs of that query names a statutory proposition. The models were routing correctly; the mode simply had no channel for the authority the question actually needed. On the control query, where the governing authority *is* case law, Claude cited *BCE Inc. v. 1976 Debentureholders*, 2008 SCC 69 and *3716724 Canada Inc. v. Carleton Condominium Corp. No. 375*, 2016 ONCA 650 — under **both** prompts, all verified. **Statutory authority in authority mode is therefore an open gap, logged to BACKLOG and not addressed here** (out of Phase 2b scope; it needs a retrieval channel, not a prompt change, since the SoW's no-invented-authority rule forbids citing statute text from training knowledge).

Safety discipline held across all six live runs: **zero fabricated citations** (no `not_found`, no `name_mismatch`, no `unsupported`), every citation carried a neutral citation in checkable form, every run still used `[AUTHORITY REQUIRED — lawyer to confirm]` (3–5 markers per memo), and every citation the models did give was correctly named and correctly characterised on inspection. `tests/acceptance/verify_citation_verification.py` passes 74/74; its `"Every citation you give WILL be checked"` anchor was deliberately preserved through the rewrite.

## 2026-08-27: Phase 2b — Authority mode and citation verification

**Context:** Matter-only mode forbids the model from invoking any external legal authority. That is safe, and it produces a memo full of `[ELEMENTS REQUIRED]` gaps. Phase 2b lifts the prohibition for Draft Memo and Draft Pleading — and catches the resulting risk, model-fabricated case citations, *after* generation rather than trying to prevent it by instruction alone. This is the *Mata v. Avianca* failure mode, and it is the one this project's governing rule exists to prevent.

**Decision: extract, then verify, then strip.** After the model produces output, every case citation is extracted, each distinct one is checked against CanLII, and the answer is rewritten with markers. Verification runs *after* generation and mutates only `answer`, which is what makes it invisible to every run that did not opt in, and what makes it structurally incapable of disturbing the code-owned pleading machinery (the DRAFT banner and cover note are applied at *render* time, wrapping whatever `answer` contains).

### The mechanism, and why search is not a fallback

CanLII resolves a case directly from its neutral citation:

```
GET /caseBrowse/en/{db}/{caseId}/     caseId = "2020onca471"
  onca/2020onca471    → 200  "2020 ONCA 471 (CanLII)"  Metropolitan Toronto CC 590
  onca/2027onca999    → 404  ← fabricated
  onca/2020onca99999  → 404  ← fabricated
```

Deterministic, one call per distinct citation. Two live-verified quirks shape the code: **`caseId` must be lowercase** (`2020ONCA471` → 404 `"Data id ... is invalid"`), and **the `databaseId` path segment is entirely ignored** (`caseBrowse/en/zzzz/2020onca471/` returns the ONCA case). The second is a robustness gift — a wrong court mapping can never cause a real citation to be reported as fabricated — but it also means the *returned* citation must be compared against the one asked for, which is why the client returns the full record rather than a boolean.

**Search is ruled out as a fallback, not merely unused.** `fullText="2020 ONCA 471"` returns *R. v. Steele*, *Koshman v. Controlex* and other cases that **cite** that decision — `fullText` searches case bodies, so a citation string matches documents mentioning it, never the case itself. A search-based check would be wrong in both directions.

### Five outcomes, and only one of them strips

A boolean would have been wrong. "We checked and it is not there", "we could not check", and "this format cannot be checked" have completely different consequences for a legal document:

| Outcome | Marker | Stripped? |
|---|---|---|
| `VERIFIED` | `2020 ONCA 471 [verified in CanLII]` | no |
| `NAME_MISMATCH` | `[CITATION MISMATCH — 2019 ONSC 4484 is "…", not "…"]` | no |
| `NOT_FOUND` | `[REMOVED — citation not verified: 2027 ONCA 999]` | **yes** |
| `UNVERIFIABLE` | `[UNVERIFIED — CanLII could not be reached to check: …]` | no |
| `UNSUPPORTED` | `[UNVERIFIED — citation format cannot be checked: …]` | no |

Only `NOT_FOUND` strips, because it is the only outcome where we affirmatively established the case does not exist. Stripping on `UNVERIFIABLE` would assert fabrication because a network call failed — a twenty-second CanLII outage would gut a sound memo and accuse the model of inventing every case in it. Stripping on `UNSUPPORTED` would delete valid Supreme Court citations because of a CanLII data-format limitation. Both would put a false statement into a legal document, which is a worse failure than the one the stripping is meant to prevent. Neither earns a tick (fail-closed is preserved); when any `UNVERIFIABLE` is present the page carries a "verification did not complete" banner.

`[2005] 2 S.C.R. 601`-style reporter-only citations have no derivable `caseId` (`2005scr601` → 404 "invalid") and, per the search finding above, no fallback. They are `UNSUPPORTED` — honestly marked as uncheckable rather than blessed or accused.

### Name mismatch — the check existence alone would rubber-stamp

The lookup returns CanLII's `title` for free, so the case *name* is compared too. This catches the failure that pure existence-checking stamps with a tick: the model writes *"Smith v. Jones, 2020 ONCA 471"*, that citation is real, but it is *Metropolitan Toronto Condominium Corporation No. 590*. Confirmed live against `2019 ONSC 4484` attributed to a fabricated "Anderson v. Baker".

Matching is **deliberately loose** — a false mismatch accuses the model of misattributing a real case, which is a serious claim to put before a lawyer. Normalisation, in order: case-fold; drop corporate and procedural suffixes (`inc`, `ltd`, `corp`, `co`, `llp`, `holdings`, `appellant`, …); drop leading `re` / `reference re`; split on the party separator (`v`, `vs`, `versus`, `c`) but **do not compare sides positionally**, because CanLII styles some cases with the parties reversed on appeal; drop tokens under four characters and generic litigation words; keep numbered-company digit strings, which are highly distinctive. A mismatch is declared only when the two names share **no distinctive token at all**. Where either side yields no usable tokens, or the model gave no name, the result is "compatible" — the check exists to catch a confident misattribution, not to manufacture doubt.

### Over-extraction deletes text from a memo

Because strict mode strips, a false extraction is not a wasted API call — it removes text from a legal document. So the court token is validated against a whitelist generated from CanLII's own 409 `databaseId` values plus the neutral-citation aliases that differ (`SCC`→`csc-scc`, `FC`→`fct`, `TCC`→`cci-tcc`, `NWTSC`→`ntsc`) and the historic courts the catalog omits (`ABQB`, `SKQB`, `MBQB`, `NBQB`, `ONHCJ`). Without it, `"the 2020 Revenue 15 report"` extracts, fails lookup, and is deleted. Extraction also swallows any parallel reporter tail into the span, so stripping `2020 ONCA 471, 149 O.R. (3d) 481` does not leave a dangling `, 149 O.R. (3d) 481` pointing at nothing, and it never touches matter-document citations (`[Condo Bylaw 6.pdf p.3]`).

**The bug worth recording:** the first normaliser stripped only a *trailing* parenthetical, so CanLII's `"2021 SCC 7 (CanLII), [2021] 1 SCR 32"` did not compare equal to the model's `"2021 SCC 7"`. A genuine Supreme Court authority was reported as fabricated and stripped from the draft — the exact failure this feature exists to prevent, produced by the feature itself. `normalise_citation` now anchors on the leading *neutral core* and discards whatever CanLII appends. Regression-tested.

### Authority mode replaces the prohibition; it does not sit beside it

`AUTHORITY_MODE_INSTRUCTION` is code-owned, like `SAFETY_PREAMBLE` and `CASE_DISCOVERY_PREAMBLE`. The matter-only prohibition is **removed** from both the preamble and the task body, not supplemented — an appended permission would leave the prompt holding two contradictory instructions ("you must not cite any case" and "you may cite cases"), and a model free to follow either has unpredictable citation behaviour, which is the opposite of the point. The swaps are anchored exact substrings checked at template load by `check_authority_anchors`, so a reworded template fails loudly at startup rather than silently leaving matter-only prohibitions inside an authority-mode prompt.

Rule 4 — `[AUTHORITY REQUIRED — lawyer to confirm: …]` — does most of the real work. Fabrication happens when a model has no permitted way to say "authority is needed here and I do not have it"; a blessed escape hatch reduces invention far more than prohibition does. Rule 7 tells the model its citations will be checked, which is honest and empirically makes models cite more carefully.

Authority mode changes **nothing** about facts: every factual claim still carries its `[FILENAME p.N]` matter citation. It is available only to `draft_memo` and `draft_pleading` (`AUTHORITY_MODE_TASKS`, code-owned so a template author cannot opt a ninth task in), defaults to off, and with it off every byte of `build_system_prompt` output is unchanged from Phase 1.

### Markers are WinAnsi-safe by necessity

The markers travel in `answer`, which is the single source rendered by the web page, the Word export **and** the PDF export. ReportLab's Helvetica is WinAnsi-encoded and has no U+2713, so a check mark baked into the answer would render as a black box in the PDF a lawyer forwards onward. The canonical marker is therefore `[verified in CanLII]`; the web layer decorates it into a green tick *after* bleach, which also avoids widening the sanitiser allowlist to `<span class>` — that would loosen what **model** output may emit in order to style text **we** inserted. One level of bracket nesting is tolerated in the marker patterns, because a reporter-only citation is itself bracketed and a pattern ending at the first `]` clips `[UNVERIFIED — …: [2005] 2 S.C.R. 601]` mid-citation.

**Export integration:** `payload.highlight_markers` is now `is_pleading or authority_mode`, so a memo highlights too. A `[REMOVED]` marker in a forwarded Word or PDF file is *more* important for a reviewer to notice than a pleading gap marker, not less — it records that the tool deleted a case the draft relied on. The disclaimer is written into the exported document's head, identical to the on-screen wording, because a file can be forwarded to someone who never saw the page it came from.

**The disclaimer's two-sentence structure is load-bearing** and is code-owned in `verification.AUTHORITY_DISCLAIMER`: *what verification confirms*, then *what it does not*. A tick a reader takes to mean "this claim is correct" is worse than no tick at all, and only the second sentence prevents that reading. It renders above the DRAFT banner on pleadings, because the risk it names is the one a reader is most likely to act on without checking.

**Alternatives considered:** existence-only verification without the name check (rejected — rubber-stamps a real citation attached to an invented case name, for free); stripping on every non-verified outcome (rejected, above); auto-correcting citations (rejected as fabrication territory, and out of scope); verifying via search (ruled out by the finding above); a per-task rather than per-query toggle (rejected — the lawyer should choose per draft, and the default should be the safe one every time).

**Consequences:** an authority-mode run adds one CanLII call per distinct citation, serial because CanLII permits one concurrent request — "parallelise within the rate limit" is unavailable. At the ~1.1 s effective interval that is 6–11 s for a typical memo and up to ~33 s for a heavy pleading, capped at `MAX_VERIFICATIONS_PER_RUN = 40` (~45 s) beyond which the excess are reported as unchecked rather than silently blessed or dropped. Verification never raises: a bug in the checking layer returns the draft with an honest "could not be completed" marker rather than losing work the lawyer has already paid for. `PipelineResult` gains `authority_mode` and `verification`; both default to off/None, so all seven other tasks are untouched.

## 2026-08-27: Phase 2a — CanLII case discovery (metadata-only, and deliberately so)

**Context:** CanLII granted API access. The obvious Phase-2 ambition — retrieve case text, inject it into context, cite from it — is not available: the API returns case *metadata* and never the text of a decision, and obtaining that text by other means (scraping) would breach CanLII's terms. The question was what an honest feature looks like under that constraint.

**Decision: the tool DISCOVERS cases; the lawyer RESEARCHES them.** The task ("Suggest Relevant Cases") reads the matter's documents, converts them into legal concepts, searches CanLII from those concepts, and returns a ranked shortlist of real cases with live links. It never states what a case held. Every layer is built to keep that boundary visible rather than merely intended:

- The notes are generated under a code-owned preamble that forbids holding-claims outright.
- The result page's first element is a non-collapsible disclaimer stating that the tool has not read any of these cases.
- The funnel counts (queries → hits → unique → filtered → enriched → shown) and the verbatim query strings are shown, so the narrowing is inspectable rather than asserted.
- `canlii.py` has no method that returns case text, and cannot be made to.

This is a smaller feature than "case research", and it is the correct one. The alternative — presenting metadata matches as if they were vetted authority — is precisely the failure mode CLAUDE.md names as the gravest this system can produce, wearing a more useful-looking hat.

### What the live API actually does, versus what its parameter names imply

Every behaviour below was established by probing the live API, and several contradict the obvious reading of the endpoint. They are recorded here because a future maintainer will otherwise re-derive them the hard way.

| Behaviour | Reality |
|---|---|
| Full-text search | **Exists.** `GET /v1/search/{lang}/?fullText=` is relevance-ranked and genuinely good. Returns metadata only. The Phase-2a brief assumed there was no search endpoint; there is, and it changed the design from date-browsing to keyword discovery. |
| Filter parameters | `jurisdiction`, `databaseId`, `decisionDateAfter`, `resultTypes` are **silently ignored** — identical result sets with and without them. Only `offset`, `resultCount`, `fullText` work. All filtering is client-side. |
| Boolean operators | **Not honoured.** `"a" AND "b"` matched *more* documents than `a b`: terms are OR-ed and AND/OR/NOT are matched as ordinary words. Quoted phrases *do* sharpen ranking measurably. |
| `resultCount` (response field) | An OR-match total in the millions. Meaningless as relevance. Never shown to the user. |
| `resultCount` (parameter) | Mandatory, capped at 100. `offset` also mandatory. |
| Result stream | Mixed `{"case"}` / `{"legislation"}` / `{"commentary"}` objects. ~79/100 were cases on a representative query. |
| 429 response body | **Invalid JSON**: `{"error": THROTTLED, ...}` — the token is unquoted. A client that parses the body before checking the status raises `JSONDecodeError` instead of handling the throttle. We dispatch on status code, always. |
| Rate-limit headers | **None.** No `RateLimit-*`, no `Retry-After`. Quota state cannot be read back; it must be tracked locally. |
| Database catalog completeness | `GET /caseBrowse/en/` lists 409 databases but **omits historic courts that search still returns cases from** — verified: `abqb`, `skqb`, `mbqb`, `nbqb` (pre-accession Queen's Bench) and `onhcj` (Ontario High Court, merged into ONSC in 1990). Unknown ids must degrade, never raise. |
| `judges` / coram | **Not exposed.** The complete case record is `databaseId, caseId, url, longUrl, title, citation, language, docketNumber, decisionDate, keywords, topics, attachments, concatenatedId`. The brief asked for judges on each card; docket number takes that slot instead. |
| `keywords` | A rich editorial catchword string (`"Property — Condominium law — Exclusive use common elements — ..."`). The single best subject-matter signal available, and the basis of stage-2 ranking. |

**Court hierarchy (SoW 4.3.1).** `canlii._KNOWN_COURTS` maps real `databaseId` values to 11 tiers, SCC first. Names are carried in code rather than taken solely from the catalog *because* the catalog is incomplete (above). Three judgement calls: `csc-scc-al` (SCC applications for leave) is **excluded outright**, not demoted — a leave decision resolves nothing about the point of law, and rendered beside real SCC judgments it reads as binding authority. Ontario tribunals (`oncat`, `onltb`, `onhrt`, …) sit at tier 8, **above every out-of-province court**, because for a condominium or tenancy matter the tribunal is the operative forum. Ontario has **no separate Small Claims Court database** — those decisions are published in `onsc`; only `nssm` and `yksm` exist nationally.

**Multi-angle query strategy.** One LLM call converts matter passages + the research direction into de-identified concepts plus 5–10 queries, one per angle from a code-owned taxonomy (`doctrinal_core`, `statutory_hook`, `factual_analogue`, `remedy`, `defence`, `forum_specific`). The taxonomy is code-owned so coverage is structural rather than left to the model's judgment on the day; the model is told to **skip** unsupported angles rather than pad to a quota, because a padded query returns confidently irrelevant cases and costs the lawyer more time than a missing angle does. Query *shape* follows the API's real behaviour: one or two quoted phrases plus three to six bare terms, and never a boolean operator. Cases are deduped on normalised citation, and the set of angles that surfaced each case is retained as a ranking signal — independent convergence is evidence.

**Targeted first, broaden if thin.** The targeted pass runs as generated. If fewer than 8 cases survive filtering, code-built broadened variants (phrase quoting stripped) run as a second pass. Only if *nothing* survives does a third pass browse the relevant courts' recent dockets — which has no relevance signal at all and is labelled as such on the page. This mirrors how legal research actually works and avoids returning noise to a question with a focused answer.

**Ontario language augmentation.** When the run is scoped to Ontario, the query prompt receives Ontario statutory and forum vocabulary (`Condominium Act, 1998`; `Residential Tenancies Act, 2006`; Rules of Civil Procedure; CAT; LTB; …) because CanLII's ranking responds strongly to exact statutory titles. Non-Ontario runs receive an explicit instruction to use *general* Canadian terminology and not to guess at province-specific statute names — we do not have equivalent domain knowledge for the other provinces, and a wrong statutory title produces a confidently irrelevant search.

**Ranking is a weighted sum, not a lexicographic sort by tier.** Court authority is the largest single weight (0.40) per SoW 4.3.1, with jurisdiction 0.20, angle convergence 0.15, CanLII's own rank 0.15, recency 0.10, and catchword overlap 0.20 added in stage 2. A strict tier ordering was rejected because it guarantees that a stale, off-point SCC case outranks a directly-on-point recent ONCA case. Recency is floored at 0.15 rather than decaying to zero: the leading Ontario authority on condominium common elements dates from 1975, and a hard age decay buries exactly the cases a lawyer most wants.

Two-stage because catchwords cost one API call each. Stage 1 ranks the whole pool (300–450 cases) on signals already in the search result — court from `databaseId`, year parsed from the citation prefix (79/79 on a representative sample, 1974–2026), angle convergence, CanLII's rank. Only the top `max_cases + 10` are enriched, then re-ranked with the catchword signal. Ranking 400 cases on catchwords would cost 400 calls; this costs ~25.

**A bug worth recording: the SCC is not a foreign court.** The first cut of `_jurisdiction_score` scored by geography, giving `csc-scc` the "federal, therefore partial match" score of 0.35 on an Ontario run. That pushed a 2019 SCC case *below* a 2024 ONSC case — binding authority ranked under persuasive. The function now scores **authority, not geography**: tier 1 is a full match in every jurisdiction, because the Supreme Court binds everywhere in Canada. Caught by a synthetic ordering test, not by the live run, which is the argument for having had one.

**A second ranking bug: "no catchwords" and "unrelated catchwords" are different facts.** The first cut of the subject-match function returned 0.0 for both. Zero for *absent* catchwords is right — a case should not be punished for CanLII's editorial coverage. Zero for *present but non-overlapping* catchwords is wrong: that is positive evidence of irrelevance, not an absence of evidence. With it zeroed, court tier and recency alone were enough to put an SCC decision on Aboriginal fiduciary duties (*Southwind v. Canada*) and two automotive class actions onto an 18-case condominium-repair shortlist. `subject_signal` now returns a signed value and applies a −0.75 × `W_SUBJECT` penalty when catchwords exist and overlap nothing. Re-running the live matter afterwards: 17 of 18 results were on-point Ontario condominium repair cases, *Southwind* gone. The remaining outlier scored *positively* on catchword overlap (a product-defect class action sharing "damage/repair/replacement" vocabulary) — a genuine near-miss rather than a ranker failure, and the note honestly said the metadata gave no clear connection.

**Observed rate-limit behaviour, which is stricter than documented.** The limiter stamps its clock when a request *completes*, so the effective spacing is interval + round-trip, measured at ~1.1 s between requests (≈0.9 req/s) against a documented ceiling of 2/s. Even at that conservative rate, a 12-call burst and a 38-call production run **each drew one 429**, absorbed transparently by the backoff. CanLII's enforcement is evidently burstier than "2 per second" implies, which is the argument for keeping the completion-stamped interval rather than tuning it up to the nominal limit: the cost is a few seconds per run, and the benefit is that a throttle almost never reaches the lawyer.

**Concurrency bug found by the timing test:** `databases()` cached per instance but was not guarded, so four threads starting together each saw an empty cache and fetched the catalog — an 8-search burst cost 12 requests instead of 9. Now double-checked under a lock.

**Ontario scope demotes rather than drops out-of-province authority** (score 0.15, not excluded). SoW 4.3.1 requires out-of-jurisdiction authority to surface *when no Ontario authority is on point*; a hard filter makes that rule impossible to satisfy. When such cases do appear in an Ontario-scoped run the page shows the SoW's exact "persuasive only — no Ontario authority located on this point" line.

**Confidentiality: matter content does not travel to CanLII.** The queries are built from de-identified concepts, and the extraction prompt forbids party names, addresses, unit numbers, file numbers, amounts and specific dates. Because a prompt is an instruction and not a guarantee, `discovery.scrub_query` re-strips emails, URLs, currency, and digit runs of 3+ in code before any query leaves the machine. Four-digit years are deliberately **kept**: `Condominium Act, 1998` is the statute's name, and stripping the year would break the most valuable term in a statutory query. The second LLM call (the relevance notes) receives the concepts and the case metadata — **not** the matter passages — because describing a case we have not read cannot possibly require privileged document text. The audit log records the exact scrubbed query strings, so it doubles as the reviewable record that nothing else was transmitted.

**Rate limiting.** A process-wide `threading.Lock` plus a monotonic clock gate, held *across both the sleep and the request*, which satisfies CanLII's 1-concurrent-request rule with no second primitive. Not asyncio: the codebase is synchronous Flask and an event loop inside a request handler would be a foreign body. The interval is 0.55s rather than 0.50s because the limit has an undocumented burst allowance and no published tolerance — three rapid calls succeeded and the fourth 429'd. 429s retry with 1s/2s/4s backoff. A mid-run throttle returns the **partial shortlist with an explicit incompleteness banner** rather than an error page.

**Daily budget.** Since the API exposes no quota state, `logs/canlii_usage.json` counts calls per UTC day: soft warning at 4,000, hard refusal at 4,900 *before* the first call of a run, leaving 100 calls of headroom so a run already in flight can finish. **Known limitation, not built:** the counter is per-machine, so a firm-level deployment sharing one key would need a shared counter and per-user budgets.

**A provider quirk, not a CanLII one:** MiMo Pro is a reasoning model whose hidden reasoning tokens are charged against `max_tokens`. Setting `max_tokens=2500` on the concept call truncated the visible completion to 872 characters, where the *same prompt with no cap* returned a complete 2,783-character block — the cap starved the output instead of bounding it. `LLMClient.complete` therefore sets no `max_tokens`, and `discovery._first_json_object` salvages truncated JSON (unclosed fences, mid-array cuts) by closing open brackets, so a completion cut short yields the queries or notes generated before the cut rather than an error page.

**Not a `PipelineResult`.** This task produces no grounded answer, no citations into matter documents, and no retrieval `top_k` — the three things `PipelineResult` exists to carry. It returns `CaseDiscoveryResult` and renders its own `case_discovery.html`. Forcing it into the shared model would have corrupted `result.html` and the Day-4d export payload, both of which assume `answer` is legal prose the lawyer can rely on; here there is no such prose by design.

**Its own safety preamble.** This is the first task that is *not* matter-only, so `SAFETY_PREAMBLE` cannot be reused — its opening sentence states that no external legal authority has been retrieved, which is false here. `CASE_DISCOVERY_PREAMBLE` is a code-owned sibling guarding a different and subtler danger: in matter-only mode the risk is inventing authority, whereas here real cases with real citations and real catchwords are in the prompt, and the model has every cue it needs to describe what they *held* — which it cannot know. A confident sentence about the holding of a real, correctly-cited case is the most believable hallucination this system could produce.

**Alternatives considered:** Retrieving case text by scraping (rejected — breaches CanLII's terms and the whole design premise); automatically augmenting Draft Memo / Draft Pleading with case suggestions (deferred — task-explicit for now, so the lawyer always knows when external material is in play); caching CanLII responses across sessions (rejected for 2a — a stale shortlist is a correctness risk and the calls are cheap); a lexicographic tier sort (rejected, above); hard-filtering out-of-province authority (rejected, above).

**Consequences:** Per-run cost is ~15–40 API calls and 8–22 seconds of wall clock, not the 5–20 calls originally estimated — the enrichment pass is the difference. At 5,000/day that is 125–330 runs, comfortable for one lawyer and thin for a firm. **Export (Word/PDF/Excel) is deliberately out of scope for 2a**: the Day-4d pipeline is built around `PipelineResult` and its structured intermediates, and wiring a fourth, structurally different payload in is real work with no stated requirement behind it. The `bleach` allowlist still excludes `<a href>`, so the CanLII links on this page are rendered by the Jinja template directly rather than through the markdown sanitiser — the 2026-06-04 markdown entry's note about revisiting that allowlist remains open, and is the right place to look when a *future* task needs model-authored links.

## 2026-08-14: Attribution is split — short in document properties, full in the document footer
**Context:** Live verification of the Day-4d Word export failed on a real matter: `The DOCX file could not be generated: exceeded 255 char limit for property, got: 'Generated by Matter Clerk on 2026-08-14, drawing on [4 filenames]. Model: xiaomi/mimo-v2.5-pro.'` OOXML caps every core document property at 255 characters and python-docx enforces it by raising, so writing the full attribution — which names *every* source file — to `core_properties.comments` turned any matter with more than two or three real filenames into a failed download. The bug was invisible in `verify_exports.py` because its fixture matter has exactly three short filenames (417 → 456 chars is the boundary; the fixture sat just under it).
**Decision:** One attribution, two renderings, both on `ExportPayload`.
- `short_attribution()` — `"Matter Clerk - {matter_name or 'export'} - {YYYY-MM-DD}"`, ~56 chars for a realistic matter — goes in **document properties**. It is written to **Comments**, not Title or Subject: Title is the document's identity and for a pleading must stay owned by `DRAFT - NOT FOR FILING - ...`, Subject carries the pleading safety statement, and Comments is the field whose actual semantics are "free-text note about this file". Provenance is a note about the file, so this is the field it belongs in — and it keeps the DRAFT assertions in Title/Subject/Category untouched, which is the part that must not be perturbed.
- `attribution()` — unchanged, full, naming every file and the model — goes in the **document body footer** (Word footer, PDF page-template footer, Excel Metadata sheet), where it appears on every page and has no length limit. The disclosure is not weakened; it moves to where it is actually read.
- **PDF gets the identical split** even though ReportLab enforces no limit of its own. The two formats must not disagree about what a property field says, and a lawyer comparing the Word and PDF of one answer must not find different metadata.
- **Excel is unchanged** — its attribution is a *cell*, which has no property limit.
- Two belt-and-braces additions, because a matter name is user-supplied and unbounded: `short_attribution` clamps itself to 255, and `docx_primitives._prop()` clamps *every* core-property write, so no caller-supplied string can raise from that layer again.
**Alternatives considered:** Truncating the full attribution to fit the property (rejected — a provenance claim that stops mid-filename is worse than a short one that is complete); dropping the property attribution entirely (rejected — a file in a DMS should identify its origin before anyone opens it); putting the short form in Title (rejected — collides with the DRAFT assertion, which is safety-critical and must own that field).
**Consequences:** The **PDF footer's 3-line cut had to go too**, and this was the more dangerous half of the bug. The footer is *drawn*, not flowed, so it had a fixed line budget and the excess was simply sliced off — with 7 files the wrapped attribution runs to 5 lines and the old code drew 3, silently discarding `Model: ...`. The footer then made a provenance claim it did not finish, which is the class of quiet dishonesty this project exists to prevent. `_fit_attribution` now shortens the **file list** instead (`"... and 1 more file"`) until the whole line fits in 4 lines, so the date and the model always survive and the reader is told how many files were elided. Word, having a real flowing footer, still lists every file. `verify_exports.py` gains `verify_long_file_list()`, a 7-long-filename matter with a deliberately overlong matter name; both original failure modes were confirmed to reproduce against it before the fix.

## 2026-08-11: Day-4d — Export to Word / PDF / Excel
**Context:** Post-Phase-1 lawyer feedback: results lived only in the browser and were copy-pasted into Word, losing formatting. Export is what makes the tool usable in practice rather than a research prototype. Scope: Word + PDF for all eight tasks, Excel additionally for the three tabular ones (Timeline, Find Entities, Compare Clauses), a ~30-minute server-side result cache addressed by a token, an attribution footer in every file, and — the safety-critical part — the DRAFT machinery surviving into exported files *more* prominently than in the web UI, because an exported file can be forwarded to a client or opposing counsel. Out: bulk export, async/scheduled delivery, cloud storage, formulas, letterheads (Phase 3+).
**Decision:**
- **PDF via ReportLab, NOT WeasyPrint — decided by measurement, not preference.** WeasyPrint was the initial recommendation (it would have reused the existing HTML render, structurally guaranteeing "the export matches the screen"). It was then **empirically tested on this machine and rejected**: it needs the Pango/GLib stack, which Windows has no sane install path for. The DLLs happen to exist (the Tesseract installer ships them, and Tesseract is already a hard dependency), and are even on PATH, but `import weasyprint` still fails with `OSError 0x7e` because Python 3.8+ no longer resolves *transitive* DLL dependencies from PATH. It works only with an `os.add_dll_directory(r"C:\Program Files\Tesseract-OCR")` shim — i.e. PDF export would silently break if the OCR install moved. ReportLab is pure Python, has no system libraries at all, and its canvas API made the diagonal watermark ~8 lines instead of a fight. The cost — hand-built layout instead of HTML/CSS reuse — is paid once in `pdf_primitives`, and it bought a **genuine capability**: page-template callbacks that draw furniture *underneath* content.
- **THE DRAFT MACHINERY IS OWNED BY THE DISPATCHER, NOT THE TASK RENDERER.** In both `docx_render.build_docx` and `pdf_render.build_pdf`, the banner and cover note are emitted by the *builder*, keyed off `payload.is_pleading` — `render_pleading` renders only the body. A future edit to the pleading renderer therefore cannot drop them, extending the Day-3.5 principle (2026-06-10: code wraps model output) one layer outward to the export dispatcher. A pleading export asserts DRAFT status **five independent ways**: full-width dark-red banner at head and foot; a diagonal watermark on every page; the bordered cover note; a per-page footer repeating the banner text; and the document's own core properties/title. In PDF the watermark and footer live in the **page template**, not the story — they are not content, so deleting content cannot remove them. `[ELEMENTS REQUIRED ...]` / `[ADDITIONAL MATERIAL REQUIRED ...]` gap markers are highlighted (yellow ground, bold red type) in both formats, via a new code-owned `pleadings.REQUIRED_MARKER` / `split_required_markers` — recognising a marker is safety machinery even though the marker *text* is template-owned drafting guidance.
- **Structured intermediates on `PipelineResult`, so Excel never parses legal content out of prose.** The three tabular tasks now emit a trailing fenced ```json block **alongside** their markdown table; `structured.extract` validates it into `timeline_rows` / `entity_categories` / `comparison_table` and **strips it from the answer**, so the web UI is visually unchanged. The seven other tasks pass through untouched (`extract` returns early on task id), so their output is byte-identical to pre-4d. Templates bumped to `version: 2`. Because the two representations are generated independently and *can* disagree, `reconcile_row_count` compares them and raises a **visible warning** on the result page and in every export rather than silently trusting either — a spreadsheet that quietly differs from the answer a lawyer approved on screen is precisely the failure this project exists to prevent. `export.tables` falls back to a strict markdown parser when the structured block is absent or invalid, and the Excel **Metadata sheet records which source was used**, so a degraded export is visible rather than merely different.
- **Dates are coerced only when complete and unambiguous — a deliberate deviation from the feature request.** The ask was "Date column formatted as dates (Excel date type, not text)". But `timeline.yaml` *instructs* the model to reproduce partial dates ("March 2024") and to note ambiguity verbatim; coercing those means inventing a day, in the column a lawyer is most likely to sort on. `_coerce_iso` accepts a short list of complete formats and refuses anything carrying hedging words ("on or about", "circa"). Complete dates become real Excel date cells; everything else stays verbatim text. Mixed column type, every cell honest.
- **Result cache: module-level `OrderedDict` + `RLock`, 30-minute TTL, LRU cap 200.** Flask's session was never a candidate — it is a signed **cookie** capped at ~4 KB and an `ExportPayload` with citations far exceeds that. SQLite was rejected because results are transient by design and persisting matter text to a second on-disk store widens the confidential-data surface for no gain. `get_result` is **lookup-only**: the original spec had delete-on-read, which would have made "Export as Word, then Export as PDF" fail on the second click; TTL expiry is the sole eviction path. Eviction is swept lazily on every put/get, so there is **no background thread** to join — which matters given the deliberate `daemon_threads = False` shutdown semantics (2026-06-04). `RLock` rather than `Lock` because `put`/`get` call `_sweep` while already holding the lock. Verified with six concurrent exports of one token.
- **Endpoint `/export/<token>/<fmt>`.** The token identifies the task, so putting the task type in the URL would create a second source of truth that could disagree with the cached payload. An expired token is the *common* case, not an error, so it renders an explanatory page with **410 Gone** rather than a bare 404; `xlsx` on a prose task is **400** and refused server-side from the same `EXCEL_TASKS` constant that drives the conditional button, so the UI and the server cannot disagree. Responses carry `Cache-Control: no-store` (a matter document must not sit in a shared cache) and a `DRAFT-` filename prefix for pleadings, so draft status is visible in a file listing.
- **Two markdown-rendering bugs found by looking at the rendered output, both legally material.** (1) `_..._` was being treated as italic emphasis, so the citation `[Imperial_Plaza_Lease.pdf p.4]` rendered as *[ImperialPlazaLease.pdf p.4]* — silently corrupting the label a lawyer uses to locate the passage and breaking string-equality with the inline cite. **Underscore emphasis is now deliberately unsupported**; asterisk emphasis is what the templates actually instruct. (2) Ordered lists were renumbered from 1, but **pleading paragraph numbers are legally significant** (a defence cross-references "paragraph 7 of the Claim"), so `Block.markers` now carries each item's original ordinal and renderers reproduce rather than re-derive it. Lazy list continuation was also added, without which a hard-wrapped pleading paragraph split in two and took its gap marker's highlighting with it.
**Alternatives considered:** WeasyPrint (rejected on the Windows evidence above — this reverses the pre-build recommendation); a single generic markdown→docx converter instead of per-task renderers (rejected by the user in favour of the dispatcher, which localises per-task layout, e.g. landscape orientation for a wide comparison); model emits structured data *only*, with code rendering the markdown table for the web UI (proposed as the single-source-of-truth option — it makes divergence impossible rather than merely detectable; **not adopted**, since the approved scope kept the markdown fallback parser, which is only meaningful if markdown is still emitted; the reconciliation warning is the mitigation); delete-on-read caching (rejected — breaks the second export); Excel comments for Compare Clauses citations (rejected — comments do not survive a Google Sheets import, which the test plan requires); a real Excel `Table` object unconditionally (falls back to plain styled headers when two compared files share a filename, since duplicate headers make Excel refuse to open the workbook).
**Consequences:** Three new pure-Python dependencies (`python-docx`, `reportlab`, `openpyxl`) and **no new system dependency** — deliberately, given how much trouble the OCR stack's Tesseract/Poppler requirements already are. Timeline / Find Entities / Compare Clauses prompts changed, so their gold-set baselines move (the other five tasks are byte-identical); a model that ignores the JSON instruction degrades to the markdown parser with a visible warning rather than failing. Exports are generated **synchronously in the request thread** (~0.1–1s); a very large matter could make that noticeable, and async generation is the tuning point if so. `tests/acceptance/verify_exports.py` runs the real generators with no Qdrant, LLM or Office and asserts against the produced bytes — 113 checks covering every format × task, the five DRAFT markers, citation fidelity, Excel typing/structure, the fallback path, TTL/LRU eviction and concurrent export. The `DOCX_RENDERERS` / `PDF_RENDERERS` / `XLSX_RENDERERS` maps are where a ninth task plugs in.

## 2026-08-06: Day-4c — Compare Clauses (per-file retrieval, model-derived attributes)
**Context:** The SoW's 8th task and the last on the matter-only side, deferred from Day 3 (a single-PDF version would have mistrained users to think of it as within-document) and from Day 4a/4b until the matter concept existed. It is structurally unlike the other seven: the retrieval shape, the prompt shape, and the output shape all differ. Scope: matter mode only, minimum 2 files, user names the clause, optional file subset. Out: ad-hoc mode, suggesting clauses to compare, cross-language comparison, redline/diff output.
**Decision:**
- **Per-file retrieval, NOT merge-by-global-score — a third retrieval primitive.** `vectorstore.retrieve_per_file_by_query(client, collections, query_vec, top_k)` returns `dict[collection, list[ScoredChunk]]`, deliberately not a mode of `search_across_collections`. Two properties the merging search cannot express: the result is **total over `collections`** (a collection with no hits is a key mapping to `[]`, because "this file was searched and yielded nothing" is the fact the comparison is built on), and **insertion order follows the caller's file order**, which becomes the table's column order. `top_k` is per collection here, so context grows **linearly** with file count — the exact opposite of Day 4b's scatter-gather, whose point is that a 20-file matter costs the same as one file. A failing collection propagates, as in 4b.
- **Two independent size limits, neither silent.** `COMPARE_MAX_FILES = 20` is a **refusal**, not a truncation to the first 20: a comparison table quietly missing documents is a wrong answer that looks like a right one. The form warns and blocks submit; `run_compare_clauses` raises `CompareClausesNotApplicable` regardless. Separately, `COMPARE_TOTAL_CHUNK_BUDGET = 40` reduces per-file **depth** as file count rises (`compare_per_file_top_k`, pure and separately testable), never file **count** — every selected file keeps its column. Floored at `COMPARE_MIN_PER_FILE_TOP_K = 3`, below which a file cannot show a clause plus enough context to compare it; past ~13 files the floor wins and the budget is knowingly exceeded (logged). At the 20-file ceiling: 3×20 = 60 chunks ≈ 30k tokens at this codebase's ~500-token typical chunk. The **effective** per-file depth is what the result page reports, so a reduced run is visible rather than showing the template's nominal 6.
- **Attributes are model-derived, not template-driven.** Indemnity compares on scope/exclusions/caps/notice; termination on notice period/cause/convenience — a hardcoded per-clause-type attribute map would fight real legal-text variability and tempt the model to shoehorn documents into a shape they don't have. The prompt gives two illustrations and instructs the model to derive rows from the named clause and from what the retrieved text of *these* documents supports. One row IS fixed: **"Clause located at"** is always first. It earns its place twice — a lawyer wants the section number, and it gives the model one designated cell in which to say "not present", instead of re-deciding that independently in every cell of a column.
- **Absent-clause detection is the model's judgment, not a score threshold.** Cosine similarity always returns a ranked top-k, so a file with no indemnity clause still returns its nearest chunks with respectable scores; no threshold separates "has the clause" from "has adjacent vocabulary" without per-clause, per-corpus tuning, and getting it wrong hides a clause that IS present. Presence is therefore **affirmative** in the prompt (rule 6): claim the clause only if a passage from THAT document contains it; never infer it from other documents having it or from a document's title. Code handles only the one genuine fact — a collection returning zero points gets an explicit `(no passages relevant ...)` block rather than silent absence.
- **Three cell states, not two.** `grounded+citation` / `"Not stated"` (has the clause, silent on this attribute) / `"Not present in this document"` (no clause at all), plus `-` filler down an absent column. Collapsing states 2 and 3 would reintroduce exactly the ambiguity the task exists to remove, so the prompt states the *reason* for the distinction (rule 8) — models follow a rule whose purpose they understand more consistently than a bare instruction. **Rule 10 forbids legal commentary or evaluation in cells**: setting the clauses side by side is the output; judging which is stronger or more favourable is the lawyer's analysis and would step outside matter-only grounding.
- **A second user-message builder, `build_comparison_user_message`.** `build_user_message`'s flat CONTEXT list cannot distinguish a file that was never looked at from one searched and found wanting, so this one emits a **FILE MANIFEST** (every document, in column order, with its passage count including zero) followed by per-document passage groups. Per-passage `[SOURCE: ...]` headers are byte-identical to every other task, so the citation pipeline is untouched. `_answer_and_build` gained one optional `user_message` param; when None (every other task) the standard builder runs. The shared REQUEST section was factored into `_request_lines` so the two builders cannot drift — verified byte-identical across all 8 templates × 16 input combinations.
- **The user's clause text IS the retrieval query, unreformulated.** `compare_clauses.yaml` carries an empty `retrieval_query` seed (every other task blends a seed with user input); a seed would pull each file's top-k toward generic contract vocabulary and blunt the distinction being drawn. The input's label is the noun phrase **"Clauses to compare"**, not the question form, because the label doubles as the REQUEST key and `"Which clauses to compare?: indemnity"` reads as noise to the model.
- **Provenance means CHECKED, not contributed** — and is relabelled to say so. `retrieved_sources` / `retrieved_file_ids` list every file searched, including ones the model marks absent, so the existing `matter_query` audit event records the full comparison set with **no audit changes**. Because "Drew on:" asserts contribution, the result page shows **"Compared across:"** for this task via a `provenance_label`; every other task's line is unchanged.
- **New `InputField` type `file_multiselect`** — the first input whose choices are **runtime data** (this matter's ingested files) rather than YAML `options`. `control: true` keeps the submitted file ids out of both the embed query and the REQUEST section via the Day-4c-a control seam. Ids are authorized against the matter with `get_file_in_matter` exactly as `file_id` is, so a tampered or foreign id is a 400, never a 500 and never a silent drop; the resulting column order follows the matter's file order, not checkbox submission order. Suppressed from the result page's request summary (raw ids, and the provenance line already says it honestly). No "select all" toggle: leaving every box unchecked already means all files.
- **Availability is code-owned in one map, enforced in four places.** `prompts.MATTER_ONLY_TASKS = {"compare_clauses": 2}` (task id → minimum ingested files) drives `available_tasks()` for the ad-hoc form, the matter form, and the CLI's `--task` choices, plus `task_unavailable_reason()` as the server-side re-check on POST in both handlers. Availability is a correctness rule, not a presentation preference, so it does not live in YAML. Compare Clauses and the single-file picker are mutually exclusive: JS hides + disables the picker, and the handler refuses a POST carrying both.
- **Result-page CSS scoped to this task.** A column per document can exceed the page width, and `base.html` sets `.answer table { width: 100% }`, which would crush the columns. A `wide` class on the answer div (this task only) makes the table scroll instead; Timeline and Find Entities render byte-identically.
**Alternatives considered:** A `merge: bool` parameter on `search_across_collections` (rejected — the return type would depend on an argument, and it still could not represent a zero-hit file); silently capping to the first 20 files (rejected by the same principle as skip-and-continue in 4b — a silent gap in legal output is the worse failure; the user restricts the selection instead); a score threshold for absent-clause detection (rejected — needs per-clause tuning and would miss legitimately but atypically-phrased clauses); a hardcoded per-clause-type attribute map (rejected — fights legal-text variability); collapsing "Not stated" into "Not present" (rejected — materially different facts for a lawyer); omitting or blanking an absent document's column (rejected — loses the transparency that the document was checked); special-casing the file checkboxes by input NAME as `limitation_confirmed` is (rejected — a second name-check where a declared type is the established pattern); reusing "Drew on:" for this task (rejected — it asserts contribution, which is false for a checked-and-absent file); a `prompt_label` field on `InputField` to keep the question-form UI label (rejected — machinery for one cosmetic collision).
**Consequences:** Compare Clauses is **web-only**, like all matter querying (the CLI has no matter verb — see BACKLOG "CLI matter query subcommand"), and is excluded from `--task` rather than failing confusingly at run time. Context now grows linearly with file count for this one task, so the two size limits are the tuning points if matters get larger. Two files with the **same filename** in one matter produce two identically-headed columns; not renamed, because the header must stay copy-exact with the citation label and diverging them would break citation verification — logged as a warning and left as a known limitation. `tests/acceptance/verify_compare_clauses.py` runs the real pipeline with Qdrant and the LLM stubbed, so the grouping, absent-file, provenance, budget, and refusal logic are checkable without Docker or an API key.

## 2026-07-09: Day-4c-a polish — OCR citation markers via the locator (+ minor fixes)
**Context:** A small deferred-cleanup batch before Compare Clauses. The substantive one: citations from OCR'd pages looked identical to citations from native PDF text (`[doc.pdf p.3]`), but the OCR'd snippet often carries character-level errors — a lawyer verifying against the source would wrongly assume a byte-exact match. The rest were trivial: httpx log noise, a sentence-transformers deprecation, and em-dashes rendering as mojibake when PowerShell reads `audit.jsonl`.
**Decision:**
- **OCR marker rides the `locator` string, not a new field.** `extract_pdf_pages` already returns `ocr_pages`; `chunk_pages` gained an `ocr_pages` param and sets the per-page locator to `"p.3 (OCR)"` for those pages (bare `"p.N"` otherwise). Because the Day-4-pre locator generalization made the locator an opaque string that every downstream consumer only interpolates — `[SOURCE: file p.3 (OCR)]` header → the model's echoed inline citation → `Citation.page_or_paragraph` → `inline()` / `[CITATIONS]` / result page, and the `(source, locator)` dedup key — the marker propagates everywhere with **two edits** (`chunk_pages`, and `pipeline.ingest_file` passing `ocr_pages` through). No change to `Citation`, the prompt, or the templates. Works in single-file and matter mode alike (matter mode reads the same stored locator). `ocr_pages` defaults to `None`, so native PDFs, the `.eml` path (`chunk_email`, own locator), and every existing caller/test are byte-identical. **Not backfilled:** collections ingested before this keep bare `"p.N"` for pages that were OCR'd — an honest transition state, since the payload text is what it is; the marker appears only on newly-ingested content.
- **Audit em-dashes → ASCII hyphens, but only in content we author.** The two limitation-signal strings in `pleadings.scan_for_limitation` switched ` — ` → ` - ` (they also feed the web refusal banner, still readable). The four `pleading_type` labels keep their em-dashes deliberately — `"Plaintiff's Claim — Form 7A"` is the real Ontario legal-form name, and they double as the `variants` keys enforced by `check_template`; the tool should not impose typography on professional convention, so one em-dash in a Small Claims audit line is accepted.
- **httpx logger quieted to WARNING** in both `web.main()` and `cli.main()` (qdrant-client requests log via httpx at INFO — `"HTTP Request: GET ... 200 OK"` per call). Failures still surface. **`get_sentence_embedding_dimension()` → `get_embedding_dimension()`** in `embed.py` (the former is deprecated in sentence-transformers; 5.5.1 has both).
**Alternatives considered:** an `is_ocr` boolean on `Chunk`/`Citation` (rejected — would thread through the payload, the Citation model, and `inline()` rendering for what the locator already carries for free); de-em-dashing the pleading labels too (rejected — real form names + `variants` keys); a UTF-8 BOM on `audit.jsonl` so PowerShell decodes em-dashes (rejected — more brittle across environments than plain ASCII, and the audit log is functional not typographic). The "SuperiorCourt" spacing glitch reported for this batch was **not reproducible** — all four labels have correct spacing in `pleadings.py`, `draft_pleading.yaml`, and the verbatim Jinja render path; no edit made against correct code.
**Consequences:** Lawyers can now see at a glance which citations came from OCR and treat those snippets as approximate. The `(OCR)` suffix is verified against the OCR'd source, so it does not weaken the citation-verification discipline. Mixed old/new collections may show the marker inconsistently until re-ingested — acceptable and documented.

## 2026-07-09: Day-4c-a — Timeline detail toggle (Concise / Detailed)
**Context:** Post-4b lawyer feedback: matter-mode Timeline reads shorter/less detailed than single-file Timeline. Root cause is structural, not a prompt weakness — the Day-4b scatter-gather truncates to a **global** top_k (14) that is then spread across N files, so per-file detail is diluted versus a single-file query that spends all 14 slots on one document. Ask: a **per-query** choice between Concise (today's behaviour, unchanged) and Detailed (more retrieval + an exhaustiveness instruction). Scope fixed to Timeline only; no other task gets a toggle, no change to Timeline's output table format.
**Decision:**
- **One retrieval knob, not two.** Detailed matter-mode retrieval is a single constant `pipeline.DETAILED_MATTER_TOP_K = 28` (~2× the template default of 14). It is passed as the ordinary `top_k` to the **unchanged** `search_across_collections`, which uses it as **both** the per-file fetch depth and the global merge cap. This deliberately keeps `per_file == global`, preserving the Day-4b **losslessness guarantee** (a global-top-k chunk is necessarily in its own file's top-k, which holds only while per-file ≥ global). The originally-floated "20 per file / 40 global" split was rejected: per-file < global silently breaks that guarantee, and 40×~700-token chunks ≈ 28k tokens. At 28 chunks the context is ~13–15k tokens typical — 2× the event budget, comfortably inside the model window. **Resolution order in `run_matter_query`:** explicit Advanced `top_k` override > Detailed (28) > template default (14). Single-file (`run_query`) retrieval is **unchanged** — already exhaustive within top_k, so Detailed there is prompt-only.
- **`detail_level` is a "control" input, not a content input.** New `InputField.control: bool = False`. A control field renders in the form and shows on the result page, but is **skipped** by `build_retrieval_query` (must not perturb the embed query) and `build_user_message`'s REQUEST section (must not perturb context) — a one-line `if field.control: continue` at the top of each field loop, placed **before** the value is read so the field is skipped whole. `timeline.yaml` declares `detail_level` (`type: select`, `control: true`, options `[Concise, Detailed]`), so the existing `_task_form.html` select rendering shows it **only under the Timeline task group** (both matter and ad-hoc) with **zero template edits**. Marking it `control` is precisely what keeps "Concise" out of both builders and makes Concise byte-identical to pre-4c-a.
- **Prompt change is a code-owned constant, threaded via `structured_inputs`.** `DETAILED_TIMELINE_INSTRUCTION` sits beside `SAFETY_PREAMBLE` / `MATTER_CONTEXT_NOTE` (so a template author can't weaken it) and is appended after the task body **only** when `structured_inputs["detail_level"] == "Detailed"`, in **both** single-file and matter modes. `build_system_prompt` reads `detail_level` straight from `structured_inputs` (the exact pattern `variants`/`pleading_type` already use) — **no new parameter**. Absent / "Concise" ⇒ nothing appended.
- **Backward compatibility = default Concise.** Missing `detail_level` (old bookmarked POSTs, CLI without the flag) resolves to Concise everywhere: identical prompt, retrieval query, user message, and top_k (14). A new CLI `--detail-level {Concise,Detailed}` flag gives parity; the CLI's input-name filter drops it for non-Timeline tasks.
**Alternatives considered:** the 20/40 per-file/global split (rejected — breaks losslessness and ~28k tokens); adding a `detail_level` **parameter** to `build_system_prompt` (rejected — `structured_inputs` already carries it, matching `pleading_type`); making `detail_level` a normal input (rejected — it would leak "Concise"/"Detailed" into the embed query and the REQUEST section, breaking Concise byte-identity; hence the `control` flag); editing `timeline.yaml`'s body for Detailed (rejected — a template author must not be able to weaken exhaustiveness, and a body edit can't be conditional per-query); a shared toggle across all tasks (out of scope — Timeline-only per the feedback).
**Consequences:** Concise output is **byte-identical** to pre-4c-a in both single-file and cross_document modes (verified offline against a reconstructed pre-4c-a template: retrieval query, user message, and system prompt across empty/focused inputs). The `control` flag is a reusable seam for future run-steering inputs. Detailed roughly doubles matter-mode Timeline context (~13–15k tokens typical); if future matters are large enough that 28 chunks strains context, the constant is the single tuning point. The 8k-token budget floated in planning was based on a wrong chunk-size assumption (chunks are ~700 tokens, so even Concise's 14 ≈ 6–10k); the real target adopted is ~13–15k typical for Detailed.

## 2026-06-25: Day-4b — cross-document retrieval (scatter-gather across a matter)
**Context:** Day-4a gave matters persistence + multi-file ingestion but tasks still queried one file at a time. Day-4b delivers the substantive value of the matter concept: a query defaults to running across **all** files in the matter, with the option to restrict to one. Scope was fixed (scatter-gather; all seven tasks matter-aware via the pipeline, not template rewrites; matter-wide limitation gate; UI default flip; audit of contributing files). Compare Clauses, deletion, sharing, and large-matter performance work stay out (4c).
**Decision:**
- **Split, don't branch.** New `pipeline.run_matter_query(files, task, structured_inputs, matter_id, top_k)` sits beside `run_query`; the **web handler dispatches** on `file_id` presence (present → `run_query`, today's single-collection path verbatim; absent → `run_matter_query`). The pipeline does not fork internally. The answer/citation/result tail common to both is extracted into `_answer_and_build`, so the two entry points cannot drift. `run_query`'s output is byte-identical to Day-4a (verified against a captured baseline of all 10 assembled prompts).
- **Scatter-gather primitive `vectorstore.search_across_collections(collections, query_vec, top_k)`** returns merged `ScoredChunk`s (each carrying its origin `collection` so the caller maps back to a `file_id`). It fetches `top_k` from **each** collection, merges by score desc, and truncates to the **global `top_k`**. Fetching `top_k` per collection is lossless for the global top-k (a chunk in the global top-k is necessarily in its own collection's top-k); scores are comparable because every collection shares the embed model + cosine distance. **The final context stays at `top_k` regardless of file count** — a 20-file matter sees the same context volume as a single file, just the best chunks across all of them. A failing collection **propagates** (not skip-and-continue): silently dropping a file from a legal retrieval is worse than a loud failure, and at 5–20 already-ingested files the case is rare.
- **Matter-aware prompts via a code-owned runtime switch, NOT template edits.** `build_system_prompt(template, structured_inputs, cross_document=False)`: when `cross_document` is true it (a) swaps the `SAFETY_PREAMBLE` opening clause "on a single legal-matter document" → "on the documents in this matter (the case file)" via `_matterize_preamble` (which **fails loud** if the clause drifts), (b) inserts a `MATTER_CONTEXT_NOTE` sentence after the preamble, and (c) applies a literal `_MATTER_PHRASES` map to the task body/variant. The map has **three** entries, ordered longest-first: `"this single\nlegal-matter document"` (the newline variant is required — `timeline.yaml` wraps the phrase across a line break and a space-only key would silently miss it), `"this single legal-matter document"`, and a bare `"single legal-matter document"` catch-all. **The YAML templates are untouched**, so the single-file path — and the gold-set baseline and all Day-3/3.5 safety testing — stays character-identical; matter-mode wording can be tuned later by the Phase-3 curator per mode.
- **Limitation gate scans the WHOLE matter.** `_scan_matter_for_limitation(matter_texts, claim_particulars)` runs the unchanged pure `pleadings.scan_for_limitation` against every file's chunks **plus** the typed particulars, attributing signals to their source via `FileLimitationSignals(file_id, label, signals)` (the particulars carry `file_id=None`, label `"(your claim particulars)"`). Chunks are scrolled **once** per request and reused for the defendant hallmarks check. `LimitationReviewRequired` gains an optional `signals_by_file` while keeping `signals` (the flat order-preserving union) as the unchanged contract for the single-file/ad-hoc banner. The refusal banner names which files (and the particulars) tripped; `limitation_files` in the audit log is the real file IDs only (sentinel excluded). More text scanned ⇒ more signals fire — consistent with the deliberately false-positive-friendly design.
- **UI: default flips to "query the whole matter."** The matter task form's file picker is now optional and collapsed inside `<details>Restrict to a specific file (optional)</details>`, with a blank "— All files in this matter —" default option and no `required`; an unchanged submit sends empty `file_id` → scatter-gather. On a refusal re-render the picker reopens (`<details open>`) with the prior file selected, driven by the existing `selected_file_id` flow-back. The result page shows a subdued, plain-text **"Drew on: …"** provenance line above Citations whenever `cross_document` is true (including when only one file contributed — honest about grounding), suppressed for ad-hoc/restricted queries. Ingest-time artifacts (OCR/unreadable/attachment/email-metadata blocks) are already `{% if %}`-guarded and matter mode passes them empty, so they suppress as whole blocks; only `pdf_sha256` (empty in matter mode) needed an added guard.
- **Audit:** the limitation event gains `retrieved_file_ids` + `limitation_files` (and uses `source=None` in matter mode). A new `matter_query` event fires **only on the whole-matter branch** (never single-file-in-matter, never ad-hoc) recording `matter_id`, `task`, `retrieved_file_ids`, `timestamp` — **no query text or particulars** (file IDs are the meaningful, non-privileged audit signal). All new fields are additive; pre-existing JSONL records stay valid (absent ⇒ null).
**Alternatives considered:** Dispatching inside `run_query` (rejected — three of its four stages differ in matter mode; a fork would muddy the path that must not regress); retrieving `top_k·N` or some ratio then merging (rejected — unnecessary; `top_k` per collection is already lossless); putting `top_k` per file into the final context (rejected — blows context and dilutes grounding at 20 files); editing the five YAML bodies to neutral wording once (rejected — would perturb the single-file gold-set baseline; the runtime switch keeps single-file pixel-identical and is reversible); skip-and-continue on a failing collection (rejected — a silent gap in a legal retrieval is the worse failure); re-scrolling chunks for the defendant hallmarks check (rejected — wasteful and opens a concurrent-write race; texts are threaded through instead).
**Consequences:** Adding cross-document behaviour required **no schema or template changes** — `files.collection` from Day-4a is exactly the scatter-gather input, as predicted in the Day-4a entry. The single-file-in-matter and ad-hoc paths are unchanged (single-file prompt output verified byte-identical). The CLI has no matter-query verb, so scatter-gather is **web-only** today (see BACKLOG: "CLI matter query subcommand"). Built for 5–20 file matters comfortably; soft caps/pagination/parallel scatter are 4c-or-later. The limitation gate now trips more often (more text), by design.

## 2026-06-18: Day-4a — the Matter concept (persistence + multi-file + matter-aware UI)
**Context:** Post-demo, lawyers asked for multi-document support: a "matter" is the set of files for one legal case (Imperial Plaza condo dispute vs. Cresthaven), files belong to exactly one matter (one-to-many). Day 4 is staged to keep risk low — **4a (this): persistence + multi-file ingestion + matter-aware UI, with NO cross-document retrieval, NO matter-aware tasks, NO Compare Clauses.** Tasks still run against one file at a time, now chosen from within a matter. 4b makes tasks matter-aware (scatter-gather); 4c adds Compare Clauses.
**Decision:**
- **SQLite (stdlib `sqlite3`, no new dependency) at the project root `matter_clerk.db`**, owned by a new `matters.py` module. Two tables, strict one-to-many, no join table: `matters(id, name UNIQUE, description, created_at, modified_at, last_queried_at)` and `files(id, matter_id REFERENCES matters, filename, file_type, content_sha256, collection, stored_path, ingest_status, ingest_error, ingested_at, created_at, UNIQUE(matter_id, content_sha256))`. `PRAGMA user_version=1` anchors future additive migration; `PRAGMA foreign_keys=ON` per connection. Phase-3 feedback storage extends this **without re-migrating these two tables** — it becomes a NEW table referencing `files`/`matters`.
- **`UNIQUE(matter_id, content_sha256)`**: the same content across matters is fine (separate rows + separate collections); the same content twice in one matter is rejected with a **specific** message ("File X is already in this matter - duplicate detected by content hash"), not a generic error.
- **Matter-scoped collection naming `m<matter_id>-<sha16>`, persisted in `files.collection`** (the DB is the source of truth, so the scheme stays changeable without migration). This is what makes "identical content in two matters → two collections" true. The **ad-hoc path is unchanged** (`day1-<sha16>`), so the two paths are provably independent.
- **On-disk matter file store `data/matters/<matter_id>/<sha16>.<ext>`**, path in `files.stored_path`. The query route runs after the upload request (and its tempfile) is gone, and `run_query` needs a real path (it re-parses email metadata for the banner on every run). Persisting the file is lower-risk than refactoring `run_query` to be path-free (a deferred future cleanup) and matches how lawyers think about a matter folder.
- **Ingest extracted into `pipeline.ingest_file`** (returns an `IngestOutcome`), shared by `run_query` and both upload paths so the ingest logic cannot drift. `run_query` gained `matter_id: int | None` that flows **only** into the audit log (`limitation_review` event now records `matter_id`; integer in a matter, `null` ad-hoc). The retrieve→prompt→cite core is untouched and still single-collection — the limitation scan still scans only the single queried document (cross-matter scan is explicitly 4b).
- **Web routes:** `GET /` (matters list), `POST /matters/new` → **PRG redirect** to detail, `GET /matters/<id>` (detail + file manifest + task form), `POST /matters/<id>/upload` (multi-file, **PRG redirect**), `POST /matters/<id>/query` (run a task on one chosen file). The today-behavior moved verbatim to `GET /ad-hoc` + `POST /ad-hoc/query`. Multi-file upload **ingests sequentially in the request thread** (no async in 4a); each file inserts a `pending` row → copy → ingest → `ingested`/`failed`+error, so a partial-batch failure leaves an accurate manifest and per-file flash messages. The task form is one shared partial `_task_form.html` (ad-hoc renders a file upload input; matter mode renders a picker of the matter's ingested files), so the two pages can't drift. `index.html` was removed (superseded).
- **`file_id` authorization:** `/matters/<id>/query` resolves the posted `file_id` via `get_file_in_matter`, which raises `FileNotInMatter` (→ 400 with a clear message) if the id is non-integer, unknown, or belongs to a different matter — URL/form tampering is a refusal, never a 500.
- **CLI:** `matter-clerk matter <create|list|add|show>` is **structurally separate** — `main()` routes to it before the flat parser runs, so `matter-clerk --pdf <file> --task <task>` is untouched (the cheap cut, taken to avoid risking the existing CLI for a Day-4a nicety).
**Alternatives considered:** Refactoring `run_query` to query a collection with no path (rejected for 4a — bigger blast radius than persisting files; revisit later); a content-only collection name shared across matters (rejected — violates "separate collections per matter"); background/parallel ingestion (rejected — premature; sequential matches the existing single-file blocking behavior on a single-user localhost tool); argparse subparsers with a flat fallback (deferred — >30 min of integration risk against a working CLI); Flask `flash` needs a `secret_key`, set to `os.urandom(24)` per process (a restart invalidating outstanding flash cookies is acceptable here).
**Consequences:** The matter file store and `matter_clerk.db` hold confidential matter data and are gitignored (`data/matters/` already was; added `*.db`). 4b is now a localized extension: tasks become matter-aware by retrieving across the matter's `files.collection` list (scatter-gather) rather than a single collection — no schema or UI re-work needed. The cost of "same file in two matters" is a duplicate embed, accepted deliberately for conceptual clarity. No matter delete/edit yet (reasonably-permanent); revisit when needed.

## 2026-06-12: .eml ingestion — generalized the page citation field to a `locator`
**Context:** Post-demo, lawyers asked to ingest Outlook `.eml` exports alongside PDFs. Emails have no pages, and the agreed citation format is `[filename.eml from <sender>, <date>]` rather than `p.N`. The whole pipeline keyed on a `page` concept (`Chunk.page`, Qdrant payload `page`, the `[SOURCE: … p.N]` prompt header, and `Citation.page_or_paragraph`), so the question was how to support a second locator style without forking the retrieve→prompt→cite path.
**Decision:**
- **Renamed the one page-specific field to a generic `locator: str`** that each ingest path fills: PDFs set `locator="p.5"`, emails set `locator="from Kevin Oskoui, 2024-05-11"`. The field is echoed verbatim into the model's `[SOURCE: …]` header and back into `Citation.page_or_paragraph`, so `Citation.inline()` renders `[file.pdf p.5]` and `[file.eml from Kevin Oskoui, 2024-05-11]` from the same code with no branching.
- **Qdrant payload key renamed `page` → `locator`.** This is a schema change: collections indexed before this change store `page` and will `KeyError` on read. They are per-file-hash dev collections — **rebuild any affected collection by re-running with `--reindex` (CLI) or "Force reindex" (web).** No migration code is written for throwaway dev collections.
- **`SAFETY_PREAMBLE` rule 1** changed from "cite in the form `[FILENAME p.N]`" to "copy the label exactly as written in the passage's `[SOURCE: …]` header." The citation discipline is unchanged: the model still cites only what was injected; it simply reproduces the locator verbatim instead of being told the page format.
- **`.eml` parsing lives in `ingest.py` as a sibling** of `extract_pdf_pages` (new `extract_email`), not a separate module — `extract_pdf_pages` keeps its name (it is genuinely PDF/OCR-specific). The token-window loop is factored into `_chunk_tokens`, shared by `chunk_pages` (per page) and `chunk_email` (whole body, no page boundary). Citation format uses ASCII `from` rather than an em-dash, deliberately: lawyers copy/paste citations into Word and briefs, and an em-dash is font- and search-fragile.
- **Body extraction prefers `text/plain`, falls back to HTML stripped via the existing `bleach` dependency.** Sender shows the display name, falling back to the bare address; date is ISO date-only. Attachments are listed by filename as a **non-blocking warning** and never read (out of scope), as are multi-email matters and quoted-content stripping.
**Alternatives considered:** A separate `ingest_eml.py` routed from `pipeline` (rejected — ~40 lines that would re-import the shared chunker; revisit if a third format arrives); keeping `page` and adding a parallel `locator` (rejected — two fields meaning the same thing, and downstream code would still branch); an em-dash separator (rejected on copy-paste durability grounds).
**Consequences:** Adding a future source type (e.g. transcripts with timestamps) is now a matter of writing an `extract_*` + a `chunk_*` that sets `locator`, with no change to retrieval, prompting, or citation rendering. The cost is the one-time reindex of pre-existing collections.

## 2026-06-11: Day-3.5 — limitation scanner widened; confirmation checkbox moved behind the gate
**Context:** During Day-3.5 testing the audit log never recorded a limitation-review event. The chain turned out to be two bugs, not a logging fault (`audit.log_event` works; it simply was never reached):
- **Bug A — the scanner under-detected.** `pleadings.scan_for_limitation` only ran over the matter-document chunks (`vectorstore.all_chunks`), not the drafter's typed `claim_particulars`, and its lexicon was limited to explicit statutory phrases (`limitation period`, `statute-barred`, `expired`, …). A real matter — the Cresthaven email chain, with a contractual **"120-day deadline"** and evidentiary-gap/laches language but only recent (2025) dates — tripped **zero** signals: the lexicon had no term for it, and the date heuristic only fires when the oldest year is ≥ `LIMITATION_YEARS` (2) old, which 2025 is not in 2026. So the gate never fired and nothing was logged.
- **Bug B — the confirmation checkbox was always visible.** `limitation_confirmed` was a standing declared input in `draft_pleading.yaml` with no `show_when`, so `index.html` rendered it on the initial form. It looked like the gate had fired when it had not, masking Bug A.
**Decision:**
- (A) Broaden `_LIMITATION_LEXICON` to cover contractual/procedural time bars: `deadline`, an **unscoped** `\d+[\s-]?(day|week|month)` numeric pattern (catches "120-day"), `within \d+ …`, `served within`, `notice period`, `laches`, `prescription`, `prescribed period`, `intervening period`, `evidentiary`. Also scan `claim_particulars` alongside the document chunks. Each signal now **names the matched text** so the lawyer can perform the analysis they are affirming.
- (B) Render `limitation_confirmed` **only** on a refusal re-render (gated on `limitation_signals` in `index.html`), never on the initial form. The refusal banner lists the specific signals.
**Alternatives considered:** Scoping the `N-day` regex to fire only near words like "deadline/serve/within" (rejected — see Consequences); keeping the checkbox standing but disabling it until a refusal (more JS state, same end result as gating the render).
**Consequences:** **The lexicon is intentionally permissive and the `N-day` pattern is deliberately unscoped — do not tighten it thinking it is too noisy.** The module's governing trade-off (stated at the top of the limitation section in `pleadings.py`) is that a false positive costs the user one extra checkbox, while a false negative can let a real, silent contractual time bar reach a filed pleading — the gravest failure class for this system. Expect this scanner to trip often on documents that mention any number-of-days period; that is by design. The date heuristic remains a staleness signal only (oldest year ≥ 2 years old), not a general date flag.

## 2026-06-10: Day-3.5 — Draft Pleading is one task with four in-file variants
**Context:** The 10th SoW task covers four pleading types (Statement of Claim / Statement of Defence — Superior Court; Plaintiff's Claim 7A / Defence 9A — Small Claims). The task dropdown must show ONE "Draft Pleading" entry with `pleading_type` as an input (SoW §2.2), not four entries — so four top-level task files were ruled out.
**Decision:** One `draft_pleading.yaml` task with a `variants: dict[str, str]` field (new, optional on `TaskTemplate`). The shared `system_prompt` carries the common pleading rules; `variants[pleading_type]` carries that type's structure/boilerplate. `build_system_prompt(template, structured_inputs)` appends the selected variant after the shared body. The four canonical type labels live ONLY in `pleadings.py`; `prompts.load_templates` calls `pleadings.check_template` to fail loudly at startup if the YAML's `pleading_type` options or `variants` keys drift from that canon.
**Alternatives considered:** Four files under `prompts/templates/pleadings/` with a second loader (cleaner per-type version history, but more surface for a demo-critical half-day — deferred until lawyers want to curate each type independently); branching logic inside one mega-prompt that shows the model all four structures (worse output, the model sees irrelevant structures).
**Consequences:** Adding/removing a pleading type touches `pleadings.py` (canon), `draft_pleading.yaml` (options + variant), in lockstep — the startup check enforces it. Per-type prompt version history is coarser than separate files would give; revisit if needed.

## 2026-06-10: Day-3.5 — DRAFT banner + cover note are code-owned and wrapped around model output
**Context:** SoW §1.4.3 requires a non-removable DRAFT mark and a cover note on every pleading. Putting them in the template prompt would make them deletable and dependent on the model actually emitting them.
**Decision:** `DRAFT_BANNER` and `COVER_NOTE` (the four §1.4.3 points as a numbered list) are constants in `pleadings.py`. The model is instructed to emit ONLY the pleading body; the CLI and web handler wrap it — banner at head AND foot (a pleading that gets printed/PDF'd must not look final at the bottom), cover note as the first rendered section. The model cannot omit them and a template edit cannot remove them.
**Alternatives considered:** Prompt-emitted disclaimers (deletable, unreliable); a post-hoc check that the model included them (fragile). Code wrapping is the only way "non-removable" is literally true.
**Consequences:** DOCX export with a visual watermark remains backlogged (no `python-docx` yet); the web banner + cover note is the Day-3.5 deliverable. The matter-only refusal markers ("[ELEMENTS REQUIRED ...]", "[ADDITIONAL MATERIAL REQUIRED ...]") are in the template body, layered on the code-owned §1.4 preamble.

## 2026-06-10: Day-3.5 — party_role derived from pleading_type, not asked separately
**Context:** SoW §2.2 lists `party_role` as its own input, but each of the four enumerated pleading types fixes the role (claims → Plaintiff; defences → Defendant). A second select would add no information and invites contradictory input ("Statement of Claim" + "Defendant").
**Decision:** Role is derived in code via `pleadings.role_for(pleading_type)`. No separate role control is presented. This is a deliberate, documented deviation from the SoW's literal input list — the requirement (the system knowing the role) is satisfied without asking the user twice.
**Consequences:** If a future pleading type does not determine role 1-to-1, this must be revisited.

## 2026-06-10: Day-3.5 — opposing-pleading requirement via affirmation checkbox (single-PDF)
**Context:** SoW §4.6 step 1 requires that a defence not be drafted unless the opposing party's pleading is present in the matter. We are still single-PDF (the matter concept is Day 4), so there is no document list to choose from.
**Decision:** For defendant pleadings the single uploaded PDF *is* treated as the opposing pleading; the user must tick "I confirm the uploaded PDF is the opposing party's pleading (Statement of Claim or Plaintiff's Claim)." Unticked → refuse (`validate_pleading_inputs`). A non-blocking pleading-hallmarks heuristic (`has_pleading_hallmarks`) warns on the result page if the uploaded text lacks typical pleading markers — it runs post-upload (scanning is only possible after ingest), so the warning surfaces on output, not in the blank form. The defence is drafted as an admit/deny response to the uploaded pleading; the defendant's own affirmative facts, absent in single-PDF mode, get the "[ADDITIONAL MATERIAL REQUIRED ...]" marker.
**Alternatives considered:** A document-picker dropdown (needs the Day-4 multi-doc matter concept); a hard heuristic gate (false positives would block legitimate drafts). The affirmation checkbox is the hard gate; the heuristic is advisory only.
**Consequences:** Evolves into a document-picker in Day 4 once a matter holds multiple documents.

## 2026-06-10: Day-3.5 — limitation check is false-positive-biased; first use of the audit log
**Context:** SoW §4.6.1 requires refusing to draft a pleading where the matter indicates a limitation-period issue, unless the user confirms a limitation analysis is done. False negatives (silently drafting past a real limitation issue) are dangerous; false positives cost one checkbox.
**Decision:** `pleadings.scan_for_limitation` scans ALL stored chunks (via a new `vectorstore.all_chunks` scroll helper — not just the top-k retrieved, since a limitation date may not be near the drafting query) with two independent trips: (1) a limitation lexicon (`limitation`, `Limitations Act`, `statute-barred`, `discoverab`, `expired`, ...), and (2) a date-age check — the oldest 4-digit year in the text being > 2 years (Ontario basic limitation period) before the current year. Either trips. If tripped and the user has not ticked the confirmation, `run_query` raises `LimitationReviewRequired` (handled at the CLI/web boundary as a notice + re-render) BEFORE the model is called. Every trip is written to the audit log (`audit.py`, JSONL at `logs/audit.jsonl`) with the signals, the pleading type, the source, the sha256, and whether the user confirmed/proceeded — the first instantiation of the §1.4.1 Part 4 audit log, which Phase 2 will extend to stripped CanLII citations.
**Alternatives considered:** Scanning only retrieved chunks (could miss an off-query limitation date — unacceptable false-negative risk); a smarter date parser (more code, and the year-granularity check is deliberately crude and aggressive, which is the safe direction).
**Consequences:** The notice fires often (most litigation files reference years > 2 years back) — intended; it is a speed-bump, not a classifier. New `InputField` types `select` and `checkbox` and a `show_when` conditional-visibility rule were added to support the pleading form (the web JS toggles conditional fields and disables hidden ones).

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

## 2026-08-30: Packaged as a PyInstaller onedir bundle (Phase 3 Session 3)
**Context:** The tool has to reach Ontario lawyers who have no Python, cannot install Docker, and in many cases cannot reach PyPI or Hugging Face through corporate HTTPS inspection. Session 1 removed the Docker dependency by moving to embedded ChromaDB. This session removes the Python dependency.
**Decision:** PyInstaller in **onedir** mode, entry point `matter_clerk_launcher.py`, console visible, built by `build_windows.ps1` from `matter_clerk.spec`.

Onefile was rejected: it re-extracts the entire bundle to `%TEMP%` on every launch, which at 542 MB is a large write and tens of seconds of startup, every single time, and it makes crash tracebacks point at directories that no longer exist. Onedir starts in five to ten seconds and can be inspected with a file manager.

The console stays visible for now. A windowed build whose Flask server dies on startup leaves the user with nothing to report; `console=False` is a one-word change in the spec once the build has been trusted.

**Alternatives considered:** cx_Freeze and Nuitka (both viable; PyInstaller has by far the most Windows-specific hook coverage for this dependency set, which is where the risk actually lives); shipping a Python installer plus a pip install script (returns us to exactly the corporate-network problem we are packaging to escape).
**Consequences:** The build must run on Windows. The output is unsigned, so SmartScreen will warn on first run and some corporate AV will quarantine it — a known, accepted cost, since code signing is deferred. `vendor/` and `models/` are gitignored; reproducibility comes from `scripts/stage_vendor.ps1` (which asserts binary versions) and `vendor/VERSIONS.txt` rather than from committed binaries.

## 2026-08-30: sentence-transformers + torch replaced by ONNX Runtime
**Context:** Packaging exposed a cost that did not matter when everything ran from a checkout: `torch` is 528 MB installed (360 MB of it DLLs), and it drags in `transformers` (102 MB), `scipy` (117 MB) and `sklearn` (43 MB). A bundle carrying all of it measured out at an estimated 1.1-1.5 GB — for a single 384-dimension embedding model. Torch is also the most fragile thing to freeze on Windows, because of its dynamic DLL loading.
**Decision:** Run `BAAI/bge-small-en-v1.5` through `onnxruntime` + `tokenizers` instead. `embed.py` reimplements the three steps sentence-transformers was performing, which the model's own `modules.json` spells out exactly: WordPiece tokenize (lowercasing lives in the tokenizer's normalizer), BERT forward pass, then **CLS pooling** — not mean pooling, and not the BERT pooler dense layer — followed by L2 normalization.

This was viable because the entire torch surface in this codebase was one 26-line file. `embed()` and `embedding_dimension()` are the only public functions, and `pipeline.py` and `discovery.py` reach the model exclusively through them.

`onnxruntime` was already installed as a chromadb dependency, so the swap adds no new transitive weight.

**The gate:** the change is only safe if it is invisible to retrieval, so it was made conditional on a test rather than on confidence. `tests/acceptance/verify_embedding_parity.py` re-embeds the document text stored in the live Chroma collections and compares against the vectors sentence-transformers wrote during earlier phases — then, more importantly, checks that top-k retrieval returns identical chunk ids in identical order for both backends. Result: 67 stored chunks, worst-case cosine 1.000000 against a 0.9999 floor; 45 query/collection pairs, zero reordering. Existing collections did not need re-indexing.

**Alternatives considered:** keeping torch and shipping a ~1.4 GB bundle (rejected: past the size the user set as problematic, and the least reliable thing to freeze); an int8-quantized ONNX model at ~33 MB (rejected: real vector drift against collections already on disk, to save 100 MB that is not the binding constraint); downloading the model on first run (rejected on the same corporate-network grounds as everything else in this session).
**Consequences:** Bundle is 542 MB instead of ~1.4 GB. `EMBEDDING_MODEL` now names the one model the build can serve; anything else raises, rather than silently returning bge vectors under another model's name and quietly poisoning a collection. Adding a second embedding model in future means exporting it to ONNX, not just changing an environment variable — a deliberate trade of flexibility for a build that ships.

## 2026-08-30: Read-only resources and writable data are resolved separately
**Context:** A packaged app installs to Program Files and cannot write there, but every path in the codebase resolved to `Path(__file__).resolve().parents[2]` — the repo root — for both the prompt templates it reads and the SQLite DB it writes.
**Decision:** `src/matter_clerk/paths.py` splits the two: `resource_path(rel)` for read-only things shipped with the code (prompt templates, HTML templates, model weights, vendored binaries), resolving under `sys._MEIPASS` when frozen; `data_path(rel)` for writable state (`matter_clerk.db`, `data/matters`, `data/chroma`, `logs`), resolving to `platformdirs.user_data_dir("MatterClerk", appauthor=False)` when frozen.

`appauthor=False` is load-bearing: platformdirs defaults appauthor to the appname, which would produce `...\Local\MatterClerk\MatterClerk`.

Resolution order for data: `MATTER_CLERK_DATA_DIR`, then platformdirs if frozen, then **the repo root if running from source**. That last carve-out is the point of the design — without it, running from a checkout after this change would silently abandon the developer's existing database, matter files and Chroma collections. Verified after the migration: every path (`db_path`, `matters_store_root`, `audit_log_path`, the CanLII usage counter, `default_store_path`, `templates_dir`) resolves byte-identically to its pre-change value in a source checkout.

Per-item overrides that already existed (`MATTER_CLERK_DB`, `MATTER_CLERK_MATTERS_DIR`, `MATTER_CLERK_AUDIT_LOG`, `CHROMA_DB_PATH`) still take precedence over all of it, so the acceptance tests were unaffected.

The data directory is created silently on first launch, and carries a `version.txt` layout marker (currently `1`). Nothing reads the marker yet; it exists so a later release can tell a fresh install from one written by an older version without inferring it from which files happen to be present.
**Consequences:** Two path functions instead of one, and the distinction has to be made correctly at each call site — but conflating them is precisely how an application ends up trying to write to Program Files.

## 2026-08-30: OCR binaries vendored into the bundle
**Context:** `pytesseract` and `pdf2image` shell out to `tesseract.exe` and `pdftoppm.exe`. Until now both had to be on the system PATH — an assumption a packaged application installed by a non-technical user cannot make.
**Decision:** `scripts/stage_vendor.ps1` copies the pieces actually used into `vendor/`: `tesseract.exe` plus its DLLs and `tessdata/eng.traineddata`; `pdftoppm.exe`, `pdfinfo.exe` (pdf2image needs both — one renders, one counts pages) plus their DLLs. `paths.tesseract_exe()` and `paths.poppler_bin_dir()` return the vendored copies when present, or `None`, which means "fall back to PATH" and preserves the old behaviour for a checkout that has not run the staging script.

Excluded to save ~35 MB: roughly 25 Tesseract training executables, `doc/`, and `osd.traineddata` (10.5 MB, needed only for `--psm 0/1`; `pytesseract.image_to_string` uses the default psm 3).

The binaries are declared in the spec as `datas`, not `binaries`. PyInstaller rewrites and relocates `binaries` entries, which would break the exe-relative discovery `tesseract.exe` uses to find its own `tessdata/` directory.

**Also vendored: the tiktoken BPE cache.** `ingest.py` builds its `cl100k_base` encoder at module import, and tiktoken fetches the ranks from `openaipublic.blob.core.windows.net` on a cache miss — so on an offline machine that is an import-time crash, not a degraded feature. The launcher points `TIKTOKEN_CACHE_DIR` at the bundled copy before anything imports `matter_clerk.ingest`.
**Consequences:** ~99 MB of the bundle. Endpoint protection that blocks process spawning will break OCR specifically, while leaving the rest of the app working — worth knowing when a managed machine reports OCR failures and nothing else.

## 2026-08-30: The launcher probes the port instead of relying on bind failure
**Context:** The intended design was to let the port bind fail when a second instance starts, and turn that failure into a readable message. Embedded ChromaDB is owned by exactly one process, so a second instance sharing the data directory is a corruption risk, not just a UX wrinkle.
**Decision:** Probe the port with a connect attempt before binding.

The original design does not work on Windows. Werkzeug sets `SO_REUSEADDR`, and where Linux rejects a second bind to a live address, **Windows accepts it** — verified empirically during this session: a second launcher bound an already-serving 5050 and started handling requests without raising. Bind failure is therefore not a usable in-use signal here, and relying on it would have let two processes open the same Chroma directory for writing.

A `connect_ex` probe that succeeds means someone is listening; the launcher prints a message naming the likely cause and how to override the port, and exits 1. The `OSError` handler around `make_server` is kept as a second line of defence and is the correct path on other platforms.

The launcher also waits for `GET /healthz` — a new route returning a fixed `{"app": "matter-clerk"}` marker — before opening the browser, rather than sleeping a guessed number of seconds. `make_server` has already bound the socket by the time the polling thread starts, so this normally succeeds on the first attempt; on a slow machine it waits longer instead of opening a browser at a connection-refused page.
**Alternatives considered:** a lock file (more moving parts, and it can be left stale by a crash; the port is already the contended resource); falling back to the next free port (actively harmful here — it would let two instances run against one ChromaDB, which is the failure being prevented).
**Consequences:** Single-instance protection is a side effect of the port being fixed. Two instances are still possible if a user deliberately sets different `MATTER_CLERK_PORT` values against the same data directory; a lock file would be the answer if that ever shows up in practice.

## 2026-08-31: Per-user Inno Setup installer (Phase 3 Session 5)
**Context:** Session 3 produced a working 673 MB folder. A lawyer cannot be handed a folder — they need one file to download, a Start Menu entry, and an uninstaller that appears where they expect it.
**Decision:** Inno Setup 7, per-user install to `%LOCALAPPDATA%\Programs\MatterClerk`, `PrivilegesRequired=lowest`. No UAC prompt appears at any point. `PrivilegesRequiredOverridesAllowed=commandline` is set so a later machine-wide IT rollout needs no script change.

The install directory is deliberately distinct from the data directory (`%LOCALAPPDATA%\MatterClerk`). Application code and privileged client material must be separable, so that removing the program never implies destroying the work.

Compression is `lzma2/ultra64` with `SolidCompression=yes`: **196.6 MB from a 673 MB bundle, 29.2%**. That is far better than the 300-400 MB estimated before measuring, because the solid stream deduplicates the ~90 MB of DLLs the bundle ships twice (see BACKLOG). Worth recording that the pre-measurement estimate was too pessimistic by roughly a third.

**Alternatives considered:** WiX/MSI (the right answer if corporate IT ever needs Group Policy deployment; far heavier to author, and per-machine MSI reintroduces the admin requirement this deployment is avoiding); NSIS (comparable, but Inno's Pascal scripting made the uninstall-data logic clearer); a zip file with a README (no Add/Remove Programs entry, no shortcut, and no way to ask the uninstall question).
**Consequences:** Unsigned, so SmartScreen warns on first run — accepted, since signing is permanently deferred. The installer must be rebuilt whenever the bundle is; `installer\output\` is gitignored.

## 2026-08-31: First-run wizard is tkinter, not a Flask page
**Context:** The app needs two API keys before it can do any LLM or CanLII work, and until Session 5 there was no way to supply them except hand-editing a `.env` file whose location the user would have to be told.
**Decision:** A tkinter dialog in `src/matter_clerk/first_run_wizard.py`, shown by the launcher whenever `.env` is absent, or on demand via `MatterClerk.exe --first-run`.

The deciding argument was not aesthetics or dependencies — it was the open bug in BACKLOG.md about `webbrowser.open()` intermittently failing when launched from a shell context. The installer's post-install step and the Start Menu shortcut are both exactly that context, and Session 5 makes it the *normal* launch path rather than an edge case. A browser-served wizard would depend on the one mechanism already known to fail sometimes, at the one moment a user has no way to recover: they would see a console window and nothing else, on install day. tkinter draws its own window with no browser involved. It looks dated. It appears every time.

Cost: tkinter's tcl/tk data adds ~4 MB and ~929 files to the bundle. Verified present in the built bundle (`_tcl_data\init.tcl`, `tcl86t.dll`, `tk86t.dll`), because a missing tcl runtime would fail precisely on the machine the wizard exists to serve.

**The wizard runs in the same process as the app.** On save it returns True and the launcher falls through into normal startup — no subprocess, no second console window, nothing to orphan. A spawn-and-exit design was rejected because a spawn that fails silently is indistinguishable from a crash, right at the moment first impressions are formed.

**Key validation uses `GET /api/v1/key`, not `GET /models`.** The session brief specified `/models`; that endpoint is unauthenticated. Measured during this session: it returns HTTP 200 for a fabricated key. Testing against it would have told a lawyer their bad key was fine — worse than offering no test at all. `/key` requires the bearer token. Network failures are reported distinctly from rejected keys, because "you are offline" and "your key is wrong" call for different actions and a corporate proxy makes the former common.

The written `.env` is locked to the current user with `icacls` (best-effort, never fatal), since it holds live credentials. The success message deliberately does not echo OpenRouter's key label, which defaults to a masked form of the key itself.

## 2026-08-31: A missing API key is a 503 with a remedy, not a bare 500
**Context:** Found while testing the Session 5 integration, and it contradicted the session brief. The claim was that the bundle "crashes with OPENROUTER_API_KEY not set". It does not: `LLMClient` is constructed lazily inside `pipeline.run_*` and `discovery`, so the app starts normally, serves the UI, and answers `/healthz` with no key at all. The failure arrives only when a lawyer actually runs a task — and `web.py` had no handler for it, so Flask rendered a **bare "Internal Server Error"** with the one useful sentence visible only in the console window behind the browser.

That is worse than a startup crash. A startup crash is at least legible.
**Decision:** `llm.MissingAPIKey(RuntimeError)` replaces the bare `RuntimeError`, and both query paths in `web.py` — matter and ad-hoc — catch it and render a 503 whose message names the remedy (`--first-run`, or the `.env` key). A named exception rather than `except RuntimeError`, so the handler cannot silently swallow unrelated runtime failures.
**Consequences:** With the wizard in place this should be unreachable on a fresh install, but it remains the correct behaviour for a `.env` that is edited, emptied, or has its key revoked — none of which are hypothetical over the life of a deployment.

## 2026-08-31: Uninstall keeps matter data unless asked twice
**Context:** The uninstaller has to answer "what happens to the lawyer's matters?" There is no undo, and the material is privileged.
**Decision:** Keep by default. `InitializeUninstall` asks whether to also delete `%LOCALAPPDATA%\MatterClerk`; `MB_DEFBUTTON2` makes "No" the default, and a "Yes" raises a second confirmation that also defaults to "No".

**This replaced the originally approved checkbox, and the reason is worth recording.** A checkbox would have to live on `UninstallProgressForm`, which renders only *after* the user has confirmed they want to uninstall — so the data question would arrive after commitment, where a single mis-click destroys client files irrecoverably. `InitializeUninstall` runs before any file is touched and can abort cleanly by returning False.

`[UninstallDelete]` is static and cannot express this, which is why the logic is in `[Code]` at all.
**Consequences:** Keeping data means a reinstall finds every matter, the Chroma index, and the existing `.env` — so the wizard correctly does not reappear. If files are locked by a running instance, `DelTree` failure is reported with instructions rather than failing silently.

## 2026-08-31: Inno's section scanner runs before Pascal comments are parsed
**Context:** The first compile of `matter_clerk.iss` failed with `Error on line 85: Invalid section tag`.
**Decision:** Use `//` line comments in `[Code]`, and never begin a line with `[`.

The `[Code]` section opened with a `{ ... }` Pascal block comment, one line of which began with `[UninstallDelete]`. Inno's section scanner is line-based and runs *before* Pascal comments are interpreted, so any line whose first non-space character is `[` is read as a section tag — even inside a comment. Recorded because the failure mode is confusing: the error points at a line that is, by every Pascal rule, commented out.

This is Inno 6 behaviour as well, not an Inno 7 change. Nothing else in the script needed adjusting for Inno 7.1.0; it accepted the Inno 6 syntax used throughout.

## 2026-08-31: The reported root cause was tested and does not hold (Session 6a)
**Context:** A pilot lawyer uploaded 28 files, ran Timeline (which returned only 2 events), then Find Facts, and got a bare Flask "Internal Server Error". The traceback ended at `chromadb.errors.InternalError: Error executing plan: Internal error: Error creating hnsw segment reader: Nothing found on disk`, raised from `search_across_collections`.

The working hypothesis was: ingestion silently succeeds on files that produce zero usable chunks, SQLite records them as ingested, Chroma holds an empty collection, and querying that collection throws.

**Decision: reject the hypothesis, and do not ship a fix predicated on it.** The state it describes was constructed and queried directly. An empty collection returns an empty result set, cleanly. Eight distinct corruption shapes were built and probed on chromadb 1.5.9:

| State | Result |
|---|---|
| Empty collection, same process | query OK |
| Empty collection, fresh process | query OK |
| Documents present, segment directory deleted | query OK |
| Segment directory present but emptied | query OK |
| Segment files truncated to 0 bytes | query OK |
| `data_level0.bin` + `link_lists.bin` removed | query OK |
| Vector segment row deleted from `chroma.sqlite3` | raises, but *"Missing vector segment"* -- different message |
| Embeddings, queue and metadata purged, segment row kept | query OK |

Chroma 1.5.x rebuilds aggressively from its SQLite metadata, so "the collection is empty" is simply not sufficient to produce this error. **A fix built on that hypothesis would have shipped, been announced as a fix, and the lawyer would have crashed again.**

**Consequences:** v1.0.1 contains the failure rather than curing it, and says so in its release notes. The root cause remains unknown. What ships instead is (a) a guard that holds regardless of cause, (b) a structure-only diagnostic the lawyer can return to us, and (c) the hygiene fixes that were worth making anyway. This entry exists mainly to stop a future session from re-adopting the discarded hypothesis because it sounds plausible.

## 2026-08-31: One unreadable file must not cost a lawyer the other twenty-seven
**Context:** `search_across_collections` iterated a matter's per-file collections and let any exception propagate. One damaged file therefore failed the entire request, for every cross-document task, permanently — the matter became unusable rather than degraded.
**Decision:** Per-collection reads are wrapped by `vectorstore._safe`, which converts a failing collection into a skip plus a named entry in a `RetrievalReport`. Applied to **all three** primitives that touch per-file collections, not just the one in the traceback:

  * `search_across_collections` — the reported crash
  * `retrieve_per_file_by_query` — Compare Clauses
  * `all_chunks_for` (new) — the pleading limitation scan, which uses `col.get` rather than `col.query`, so guarding the query path alone would have left it live

`_safe` catches broadly, deliberately. Chroma surfaces corrupt segments as `InternalError` with several different messages, and the set is version-dependent; narrowing to known strings would reopen the exact failure the guard exists to prevent.

**Two properties the guard must preserve, both of which cost something:**

*Skipping is only acceptable because it is surfaced.* `retrieve_per_file_by_query`'s docstring previously argued the opposite — "a silent gap in a legal retrieval is worse than a loud failure" — and that reasoning still holds. What changed is that the gap is no longer silent: skipped files are named in a banner **above** the answer, so a lawyer knows the result is partial before they rely on it rather than after. The docstring was rewritten rather than deleted, because the original concern is the one that constrains the design.

*All collections failing raises rather than returning empty.* "Nothing matched your question" and "none of your files could be read" must never look alike: the first is an answer, the second is a broken matter.

The limitation scan gets a stronger treatment still — an unscanned file produces an explicit warning that the review did not clear it, because that gate exists to catch a time-barred claim and a file it could not read is a file it could not clear.

## 2026-08-31: Ingest verifies against the store, not against its own counter
**Context:** Two related holes. `recreate_collection` runs *before* `upsert_chunks`, so an ingest that dies between them leaves a registered but empty collection. And `needs_index` was computed from `collection_exists`, so a re-upload of that file saw the collection, took the cache path, skipped indexing entirely, and reported success — leaving SQLite claiming "ingested" with nothing behind it.
**Decision:** Ingest now asks the store what happened rather than trusting an in-process count.

`IngestOutcome.chunk_count` **cannot** be used as the failure signal: it is 0 for a legitimate cache hit as well as for a failed ingest, so keying off it would mark every re-upload of an already-indexed file as broken. `collection_doc_count()` interrogates the collection instead. After indexing, a collection reporting zero documents is deleted and `PdfHasNoText` raised, so a broken collection cannot outlive the failed ingest that created it. On the cache path, a cached collection holding no documents forces a re-index rather than being trusted.

## 2026-08-31: Extraction quality is graded, with thresholds calibrated on real files
**Context:** "Timeline extracted only 2 events from 28 files" was the lawyer's other complaint, and arguably the one they actually felt. Files were being indexed with OCR output far too poor to answer from, and nothing anywhere said so.
**Decision:** `assess_extraction()` grades every ingest as `ok`, `ocr_low_quality`, or `failed_no_text`. Thresholds were measured, not guessed, against the nine real scanned and native matter files in the repo:

    good files: 811-4,006 chars/page, legible-character ratio 0.996-1.000

Hence **< 150 chars/page** (a 5x margin below the worst good file) or **< 0.85 legible ratio** (a wide margin below the worst) marks low quality. Verified to produce zero false positives across all nine.

Only OCR'd documents can be graded low quality: a native-text PDF that is genuinely short is short, not damaged, and flagging it would train the lawyer to ignore the badge. A low-quality file stays indexed and queryable — it is a warning, not an exclusion, because whether a clearer scan is worth chasing is the lawyer's call.

Deliberately permissive: a false "low quality" on a sparse covering letter is an annoyance; a false "fine" on 28 unusable files is the bug being fixed.

## 2026-08-31: Startup migration heals manifests, and can never block startup
**Context:** Installs already in the field carry files marked `ingested` whose collections cannot be read. Those files keep being handed to the query path forever, so the fix has to reach existing state, not just new ingests.
**Decision:** `maintenance.run_startup_migrations()` runs in the launcher before the server, guarded by a marker file per migration.

Every failure mode is a no-op that retries. **If the store will not open, the manifest is left completely alone** — demoting every file in every matter because Chroma is momentarily unavailable would be far more destructive than the bug. The marker is not written, so it retries next launch; the existing store-health banner already covers the user-visible side. The whole call is wrapped: turning a degraded install into a dead one is strictly worse than the problem being fixed.

The one-time notice goes through `<data_dir>/notices.json` rather than a schema change — the migration runs before Flask, may run when no browser is open, and must not require altering the table it is repairing.

## 2026-08-31: A diagnostic that is safe to send without being read first
**Context:** With the root cause unknown, the fastest route to it is the state of an affected machine. That machine holds privileged client material.
**Decision:** `maintenance.build_diagnostic_report()` emits structure only: version, platform, per-file ingest status, collection document counts, and a live probe classifying each collection as ok/missing/empty/unreadable. It **excludes** document text, chunk text, matter names, file names, paths that could carry a client's name, and API keys. File names are reduced to extension and character count.

The constraint driving the design is that a lawyer must be able to send it without auditing it. Anything requiring review before sending would not get sent.

Reachable from a button on the matters page **and** from the error page, per the requirement that a tool nobody can find does not exist. A command-line invocation would not have been used.

## 2026-08-31: Auto-update, failing closed in every direction
**Context:** v1.0.0 had no update path, so every fix needs manual redistribution to every lawyer, forever.
**Decision:** A background check against the GitHub releases API at startup, offering the update on the matters page only.

Version comparison is numeric per component and **case-insensitive on the leading v** — the existing release is tagged `V1.0.0` with a capital V, which a naive parser silently never matches. Lexical comparison is wrong for the same class of reason: `v1.0.10` must sort above `v1.0.9`.

Every failure is silent with a one-hour backoff: offline, proxied, rate-limited, malformed JSON, or a release with no installer attached. An update checker that interrupts legal work to complain about its own connectivity is worse than none. Nothing installs without explicit confirmation, and the notification is confined to the matters list — an offer to close the application mid-draft is an offer to lose work.

The acknowledged risk: a bug in the updater is the hardest kind to fix remotely, because it breaks the mechanism you would fix it with. Hence no clever behaviour anywhere in it.

## 2026-08-31: No lawyer sees a raw traceback page
**Context:** The field report arrived as a screenshot of Flask's "Internal Server Error". Whatever else fails, that page should not be what a legal professional meets mid-matter.
**Decision:** An app-wide error handler renders an explanatory page with next steps and a diagnostic button, and writes the full traceback to the audit log with matter id, task and path. `HTTPException` passes through untouched so 404s keep their meaning.

## 2026-08-31: File scoping is one control, not two (Session 7)
**Context:** The lawyer asked for Compare Clauses' file picker on every task. Investigating found the brief's premise was half wrong in a useful way: `_task_form.html` is already a single generic form driven by each task's YAML `inputs`, `file_multiselect` is already a generic input type, and single-file restriction (`file_id`) already worked on every matter-mode task. Compare Clauses had no bespoke template code at all. What was genuinely missing was multi-file *subset* selection, gated behind one line: `if task == pipeline.COMPARE_TASK_ID`.

So no abstraction needed extracting. What needed fixing was a design flaw the generalisation would have multiplied.

**Decision:** Replace BOTH existing controls with one three-mode selector (`_file_selector.html`): all / selected / single.

The two controls could contradict each other. A form could submit `file_id=7` *and* `file_ids=[3,4]`, and the server resolved it with `if file_id_raw: ... else: subset` — silently discarding the checkboxes. Generalising the subset to all eight tasks would have put that contradiction on every form in the application. A silent drop in a legal tool is the failure mode this codebase rejects everywhere else, so the controls were merged rather than multiplied.

Radio buttons rather than a select, for the same reason `authority_mode` is a radio: the lawyer should be able to see that scoping exists, and that "all files" is the default, before choosing.

**Consequences:** `file_ids` left `compare_clauses.yaml` entirely — file scoping is no longer a per-task prompt input but a property of matter mode, read straight off the form by `web.py`. Compare Clauses keeps every behaviour it had (subset selection, the 20-file cap, no single-file mode); only the control moved. Its client-side count gate now reads the shared control; the cap was already enforced server-side in `pipeline.py` and still is.

`run_matter_query` did **not** gain a `file_ids` parameter. It already takes `files: list[MatterFile]`, so scoping is passing a shorter list; adding an id parameter would have duplicated the matter-ownership authorization that belongs in the web layer, where it already lives.

## 2026-08-31: Unqueryable files stay visible in the selector
**Context:** Files marked `failed_no_text` cannot be searched. The selector could hide them or show them disabled.
**Decision:** Show them, greyed out, with the reason and a pointer to Re-process.

A document that silently vanishes from a list makes a lawyer wonder whether they imagined uploading it. A greyed row saying "cannot be searched — needs re-processing" turns an invisible gap into a visible, fixable one. This is the same reasoning as the Session 6a result banner naming skipped files.

Three states, not two: `ocr_low_quality` is *queryable* and stays selectable, flagged rather than disabled. Disabled inputs are not submitted, and every submitted id is still authorized server-side, so the UI state is a convenience and never the enforcement.

## 2026-08-31: A v1.0.1 inconsistency, introduced in Session 6a
**Context:** Session 6a added `ocr_low_quality` and `matters.is_queryable()`, and updated the whole-matter path to use it. It did **not** update the single-file path (`web.py:619`) or the Compare Clauses subset path (`:663`), both of which still tested `!= "ingested"`. The selector's options meanwhile came from `queryable`, which includes `ocr_low_quality`.

Net effect in v1.0.1: a lawyer could pick a "Poor scan quality" file from the dropdown and be refused — "is not successfully ingested" — for a file that searching the whole matter happily included.
**Decision:** Both paths now use `matters.is_queryable()`. One predicate, one meaning of "searchable", used everywhere.
**Consequences:** Worth recording as a pattern, not just a fix: adding a status value is not done until every branch that tests status has been found. A grep for the old literal would have caught this at the time.

## 2026-08-31: Date-prefix sorting, calibrated on the pilot lawyer's real filenames
**Context:** Lawyers name matter documents by date, and the file list was in upload order.
**Decision:** `matters.parse_date_prefix()` reads a leading date, and `matters.sort_files()` orders dated files chronologically with undated files alphabetically after them.

The rules were calibrated against the actual filenames in the pilot matter rather than invented. Those turned out to include a convention neither the brief nor the proposal anticipated — **date ranges**, in two forms:

```
2024-04-01 to 2026-04-30 - email exchange re. Heat pump.pdf
2026-01-21 - 2026-03-26 - Condo manage email re inspection.pdf
2026-03-27 - Technician Report Form.pdf
Condo Bylaw 6.pdf
```

Note the second: the same " - " separates the two dates *and* the date block from the description. Ranges sort by their start date, so a plain prefix parser would already order all of these correctly — the range is recognised anyway so the parsed value is honest and a future date column has real data.

**What deliberately does not parse**, and why the refusals matter more than the matches:

* `3-15-24_letter.pdf` — US order. Read as YY-MM-DD it is month 15, so it is rejected by date validation. US order is never *attempted*: `03-04-05` is valid in three different orderings, and silently guessing wrong in a legal chronology is worse than not sorting at all.
* `letter_24-03-15.pdf` — not a prefix. A mid-name number is as likely to be a court file number, a docket, or an amount.
* `23_march_letter.pdf` — is 23 a day or a year?

Two-digit years pivot at 70 (00–69 → 2000s, 70–99 → 1900s), since a matter may reference a 1998 document.

"Newest first" reverses the dated group only; the undated tail stays A–Z, because reverse-alphabetical is not something anyone asked for and reads as a bug.

**Consequences:** One ordering feeds the file list, the selector, and Compare Clauses' column order, so "the third file down" means the same thing everywhere. Compare Clauses columns are therefore chronological rather than upload-ordered — the one behaviour change in this session for a lawyer who touches nothing, disclosed and accepted.

## 2026-08-31: Sort preference is a preference, not matter data
**Context:** The preference had to persist per matter, but Session 7 was otherwise migration-free.
**Decision:** `<data_dir>/ui_prefs.json`, keyed by matter id, via `maintenance.get_matter_sort` / `set_matter_sort`.

A `matters` table column would have meant a schema change plus a migration on every installed copy, for a display preference that is disposable — losing it costs one dropdown click. Same reasoning as `notices.json` in Session 6a: data-directory JSON is the right home for state that is neither matter content nor worth migrating for. Writes never raise; a preference that fails to save is not an error worth showing a lawyer.

## 2026-08-31: Run scope is reported separately from provenance
**Context:** The result page already had a "Drew on" line listing files that grounded the answer. Scope is a different fact.
**Decision:** A banner above the answer states what the run was scoped to — "Ran against 5 of 28 files: …" — distinct from the provenance line below it.

Scope is what the lawyer *chose*; provenance is what actually contributed. A file can be in scope and contribute nothing, and conflating the two would let a narrowed run be mistaken later for a complete one. Placed above the answer for the same reason as the Session 6a incomplete-retrieval banner: the reader needs to know the shape of the run before reading its conclusions.

The audit log records `scope` and `scoped_file_ids` **only when the scope is not the default**, so records from default runs keep exactly the shape they had before Session 7.

## 2026-08-31: Support report gains a README and loses its heading
**Context:** Session 6a put the diagnostic behind a "Having trouble?" section on the matters page. Feedback was that it needed to be findable without being alarming, and that a lawyer handed a JSON file has no idea whether it is safe to send.
**Decision:** A quiet "Generate support report" link at the foot of the matters page with explanatory hover text, and a plain-English README written beside every generated report saying what to send, to whom, what it contains, and — the part that decides whether it gets sent — what it does not.

(For the record: the report was never CLI-only. It shipped in v1.0.1 as a button on the matters page and on the error page. What changed here is tone, placement, and the README.)

## 2026-09-01: The "not detailed enough" complaint was a global top-k cap (Session 8)
**Context:** The lawyer asked for exhaustive Timeline and Summarize because the output "isn't detailed enough — we don't want the software to decide what to include and exclude." The natural reading is a prompt problem.
**Decision:** Measure before building. It was not the prompt.

`DETAILED_TIMELINE_INSTRUCTION` has said "Capture EVERY dated event and material action... Do not summarize or omit events" since Day 4c-a. The defect is upstream: `search_across_collections` retrieves top-k from each file and then **merges to a GLOBAL top-k**, so a Timeline sends `top_k` chunks for the entire matter regardless of file count.

Measured on the 9-file dev matter (67 chunks, 31,806 tokens):

| mode | chunks to model | coverage |
|---|---|---|
| Timeline Concise | 14 | 20.9% |
| Timeline Detailed | 28 | 41.8% |
| Summarize Standard | 12 | 17.9% |
| Find Entities Standard | 16 | 23.9% |

Because the cap is a matter-wide total, a 28-file matter still gets 14 chunks — about 7% coverage. This is also the second half of the Session 6a report ("only 2 events from 28 files"), the first half being bad OCR. The model was told to be exhaustive; it was never shown the documents.

**Consequences:** Exhaustive mode is the sanctioned fix. The caps on Concise/Detailed/Standard were deliberately NOT raised, because that would break the byte-identical guarantee for lawyers who touch nothing — but "Detailed" remains much weaker than its name suggests, and that is recorded here rather than left to be rediscovered.

## 2026-09-01: Exhaustive is a sibling path, not a mode of the standard one
**Decision:** `run_exhaustive_matter_query` sits beside `run_matter_query` rather than adding a branch inside it. The two differ in exactly one thing — what reaches the model — and a branch in the middle of the shared retrieval path is a branch that can misfire into the standard modes.

They share the tail. `_answer_and_build` was split so that generation (`_ask_model`) and everything after it (`_build_result_from_answer`: citation extraction, verification, structured intermediates, PipelineResult assembly) are separable, and exhaustive mode supplies a `precomputed_answer` and reuses the rest unchanged.

Batch user messages are built by the SAME `build_user_message` the standard path uses, so the `[SOURCE: ...]` headers and CONTEXT format the model sees are byte-identical. Exhaustive mode changes which passages are sent, never how they are presented — citation behaviour is therefore unchanged by construction rather than by testing.

**Byte-identical guarantee, asserted not assumed:** the exhaustive instruction is appended only when `is_exhaustive()` is true AND the task is one of three, keyed off `template.id`, so a stray `mode` value on another task cannot alter its prompt. Twelve prompt-assembly cases cover the matrix.

## 2026-09-01: Batching is an overflow path, not the architecture
**Context:** The brief assumed the context window could not hold a matter and specified a batching pipeline with cross-batch aggregation and fuzzy deduplication.
**Decision:** Single pass by default; batch only above `INPUT_BUDGET_TOKENS = 400,000`.

The measurements do not support batching at realistic sizes. `xiaomi/mimo-v2.5-pro` and `anthropic/claude-opus-4.7` both offer ~1,000,000-token windows, and the whole dev matter is 31,806 tokens of chunk text. The budget is set at 400k rather than near the limit because a reasoning model's quality degrades well before its context limit does — batch early rather than serve a degraded single pass.

Single pass is also what makes deduplication a non-problem in the normal case: one call sees the whole email chain, quoted replies included, and merges them itself.

Batching keeps files whole unless one file alone exceeds the budget, because splitting a document across calls is what creates cross-batch duplicates in the first place.

## 2026-09-01: Deduplication is per-file and exact; cross-file is off
**Decision:** Collapse exact repeats within a single file. Never merge across files. Never fuzzy-match.

Within one file an exact repeat is an artefact of `CHUNK_OVERLAP_TOKENS = 100`, not a second occurrence. Across files it is usually two pieces of evidence about the same fact, and collapsing them destroys corroboration a lawyer needs to see.

Fuzzy matching is the wrong instrument regardless. "Notice served on tenant" and "Notice served on landlord" score ~90% similar by any string metric and are opposite facts. Any threshold loose enough to merge genuine restatements is loose enough to merge distinct events — and a silently merged event is a MISSING event, which is precisely the complaint this feature exists to answer.

**Suppression is always stated, never inferred from silence.** The run summary says "N exact per-file duplicates collapsed" or "No per-file duplicates were collapsed." The presence of the count is the safety signal; an absent line would be indistinguishable from a line nobody wrote.

## 2026-09-01: Exhaustive runs are background tasks, decided by measurement
**Context:** The initial proposal argued no async layer was needed, on the reasoning that exhaustive is one model call and the existing synchronous POST already serves Detailed.
**Decision:** That was wrong, and one real run settled it. A single-batch exhaustive Timeline over 9 files took **169.5 seconds**; a 28-file matter extrapolates to six to nine minutes. Far past what a form POST should hold open.

Runs execute on a background thread with state in `<data_dir>/runs/<id>.json`, polled every 1.5 s. The file is the source of truth, not the thread, which is what lets the browser close and reopen and lets a run interrupted by a restart report itself as INTERRUPTED rather than showing a progress bar that will never move.

One run per matter, via a lock file. A second attempt redirects to the run already going rather than erroring — a lawyer who clicks twice wants to see the run, not be corrected.

**Two bugs found by running it, both worth recording:**

*Windows `os.replace` is not reliable.* It raises PermissionError (WinError 5) intermittently when a scanner or concurrent reader holds the destination, and it killed a run before its first batch. Now a bounded retry with an in-place fallback: a torn state file read by one poll is a far smaller problem than a run that dies at startup.

*A staleness check needs a heartbeat, not batch boundaries.* Progress was reported only between batches, so during a 170-second single batch the timestamp went stale and `load()` declared its own live run dead. A 20-second heartbeat thread now runs for the life of the task. The general lesson: a liveness check whose resolution is coarser than the work it supervises will mistake slow for dead.

## 2026-09-01: Exhaustive runs are pinned to a different model, and say so
**Decision:** Exhaustive runs use `anthropic/claude-opus-4.7` and ignore `MODEL` from `.env`. The model is recorded in the run metadata, named in the pre-run dialog, and shown on the result.

**This departs from CLAUDE.md and the SoW**, which specify MiMo Pro as the default provider, and the departure is recorded here deliberately. It applies to exhaustive runs only; every other task honours the configured model.

Note for anyone re-deriving this: the id uses a **dot**. `anthropic/claude-opus-4-7` does not exist on OpenRouter and 404s on every call — verified against the live model list before the constant was written.

## 2026-09-01: Cost estimates are measured, and cl100k is not Claude's tokenizer
**Context:** A cost dialog that reads low is worse than no dialog.
**Decision:** Estimate from the actual assembled prompt, then correct for the tokenizer.

The first estimator counted raw chunk text and came in 17% under a real run: the `[SOURCE: ...]` header on every chunk plus the system prompt added ~23k tokens to a 31,806-token matter. Fixed by estimating from the same builders the run uses.

That still left it 36% low, and the cause is more interesting: **`tiktoken`'s `cl100k_base` is OpenAI's tokenizer, and Anthropic's is different.** On this content — OCR'd legal correspondence, dense in headers, dates and punctuation — a measured run counted 34,653 by cl100k and was billed 54,542. A ratio of 1.57. `CLAUDE_TOKEN_INFLATION = 1.65` applies a rounded-up correction for Anthropic models, from a single data point, kept as a named constant with the measurement in the comment rather than buried in a formula.

With both corrections the estimate brackets reality: $0.42–$0.79 shown against $0.6974 billed.

**The live tally matters more than the estimate.** "Here is what you have spent so far" answers the lawyer's real question better than any pre-run band, and it comes from the provider's own usage figures rather than from our arithmetic.

## 2026-09-01: Preview labelling names the mode, not the task
**Decision:** Summarize and Find Entities offer "Exhaustive (preview)"; Timeline exhaustive ships as GA.

The copy is careful in one specific way: it says the exhaustive **mode** is under evaluation, not that Summarize or Find Entities are preview features. A lawyer who concludes that Summarize itself is provisional would stop trusting a task that has been stable since Phase 1.

`is_exhaustive()` matches on `startswith("Exhaustive")` rather than equality, so the visible label can change without silently switching the mode off.

## 2026-09-02: python-docx silently drops tracked insertions (Session 9)
**Context:** The brief assumed "extract only accepted text" came free from python-docx. It does not, and the failure is silent and one-directional.
**Decision:** Extract text by walking `w:t` descendants of the paragraph element, skipping `w:delText`.

`Paragraph.runs` returns only `<w:r>` elements that are DIRECT children of `<w:p>`. A tracked insertion nests its run inside `<w:ins>`, so `Paragraph.text` never sees it. Deletions use `w:delText`, which `.text` also skips. The net effect is that python-docx returns *neither* insertions nor deletions — the original text, not the accepted text.

Demonstrated during design on a document built for the purpose: a paragraph of "PLAIN + INSERTED + DELETED" yields only "PLAIN".

For a legal tool this is the difference between indexing a contract and indexing a superseded draft of it. An amended agreement would be searched with its amendments missing, answers would be grounded in the wrong text, and nothing anywhere would say so. Walking `w:t` and excluding `w:delText` gives exactly the all-changes-accepted view: insertions in, deletions out.

**Consequences:** Locked in by a test that asserts both directions on a purpose-built document, including an assertion that python-docx *alone* still loses the insertion — so if the library ever changes behaviour the test says so rather than quietly passing for a new reason. Note for the record: `docs/SoW.docx` contains 22 `<w:ins>` elements but they are paragraph-mark insertions carrying no text, so that file does not exercise the bug.

## 2026-09-02: Word chunks are heading-bounded and packed to the same token target
**Decision:** Walk the document body in order, tag each paragraph and table with the heading path in force, then pack blocks to ~700 tokens without crossing a heading or table boundary.

Two library behaviours forced the shape. `document.paragraphs` and `document.tables` are separate collections with no interleaving, so reading them in turn puts every table at the end of the document and attributes it to the wrong heading; the body XML is walked instead. And 52 of 199 paragraphs in this project's own SoW.docx have `style is None`, so style access is guarded everywhere.

Measured on that file: 47 heading-bounded sections, median 86 tokens, **max 448 — none exceed 700**. One chunk per section is the normal case and splitting is the rare path. When a section does exceed the budget it splits into parts that all keep the section locator plus "(part 2 of 3)", so a citation still names something a lawyer can turn to.

Tables split on whole rows only. Half a row of a party schedule is worse than no row: the columns stop lining up and the fragment reads as a different fact. Continuation chunks repeat the header row.

Headings are emitted as content, not only as metadata — a clause is often named nowhere else, and dropping the heading loses that name from the index.

## 2026-09-02: Excel chunking sized by tokens, not rows -- and the header is not row 1
**Context:** The brief specified 50 rows per chunk and a row-1 header. Both were wrong against the pilot lawyer's actual spreadsheets, and the design was calibrated on those two files rather than on a hypothetical budget sheet.

    Submission List - Invoice   521 rows x 15 cols   76.8 tokens/row
    Invoice on May 20, 2026     379 rows x 82 cols   63.5 tokens/row

**Decision, four rules, each replacing an assumption:**

*Pack to ~700 tokens, not to a row count.* Fifty rows would have produced 3,176–3,839-token chunks against the 700-token target every other format uses. Oversized chunks distort cosine ranking against normal chunks, consume the top-k budget (one such chunk exceeds five PDF chunks), and break the ~475-token-per-chunk assumption in Session 8's batch planner. Packing by tokens lands at ~8 rows for these files.

*Score for the header row; do not assume row 1.* In `Invoice on May 20, 2026` rows 1–2 are a title and a description and the real header is row 3. A row-1 assumption would have stamped "A. LIST OF STUDENTS..." onto every chunk of that sheet as its column list. A score over breadth, brevity and non-numeric content separates them cleanly: 56 vs 43 in the first file, 72 vs 56 vs 0 in the second.

*Drop empty columns.* That sheet reports 82 columns and populates 18. Rendering all 82 makes every row two-thirds empty delimiters.

*Skip empty rows; do not split on them.* Every empty-row run in both files is a single row, and singles occur inside the data. The brief's "5+ consecutive rows is a boundary" would never once have fired.

**The header line is repeated in every chunk**, and the reason is not only retrieval quality. Session 8's exhaustive Timeline has to know which column holds dates; a chunk of bare values gives the model no way to tell a date of birth from an invoice date, so downstream extraction fails on unlabelled data. The repetition costs ~15–20 tokens and buys the chunk its meaning.

Dates are rendered as plain ISO dates rather than `str(datetime)`, which would put "00:00:00" on every date cell — noise in the index and misleading in a citation.

## 2026-09-02: Locators must be navigable, not merely unique
**Decision:** Excel citations carry sheet name, real 1-based Excel row numbers, and the first five column headings.

```
[Student list.xlsx sheet 'Invoice on May 20, 2026', rows 15-24
 (cols: Student ID | Last Name | First Name | DOB | Gender ...)]
```

Row numbers alone are unique but uninterpretable: a lawyer reading "rows 15-24" learns nothing about what is in them. The column list is what makes the citation checkable, truncated so a wide sheet does not produce a citation longer than the passage it labels. Row numbers match Excel's own numbering so the file can be opened at that row.

Word uses `§<heading>` for prose and `Table N "caption", rows A-B` for tables, falling back to `¶N` where a document has no headings.

## 2026-09-02: Encryption and corruption are distinguished by file header
**Context:** python-docx raises `PackageNotFoundError` and openpyxl raises `InvalidFileException` for BOTH encrypted and corrupt files, so the exception type cannot tell them apart — but the remedies are completely different.
**Decision:** Sniff the first eight bytes. An encrypted OOXML file is an OLE2 compound document beginning `D0 CF 11 E0 A1 B1 1A E1`; a healthy one is a zip beginning `PK`.

New status `password_protected`, separate from `failed` and not queryable, with a message naming the fix: remove the password, save a copy, upload that. A corrupt file keeps `failed` and says it may be damaged or in an older `.doc`/`.xls` format.

No new dependency — `olefile` is not installed and is not needed for an eight-byte check.

**Also distinguished:** a workbook whose formulas have no cached values, which happens when a file is written by a program and never opened in Excel. `data_only=True` returns `None` for every such cell, so the sheet would index as empty. It is reported with its own remedy (open and save in Excel) rather than as a damaged file.

## 2026-09-02: PDF and email paths were held byte-identical, and proved so
**Decision:** The `is_email` boolean became a suffix dispatch with Word and Excel as new branches *beside* the existing ones, never inside them.

The guarantee is asserted rather than assumed: chunk lists for the nine real matter PDFs were hashed on the pre-Session-9 tree and the digests pinned into `verify_docx_xlsx_ingestion.py`. All nine match after the change. Adding a dispatcher is exactly the kind of edit that perturbs an existing path by accident, and a hash is the only check that would notice a one-character difference in a locator.

## 2026-09-02: Footnotes deferred
**Context:** The brief asked for footnotes extracted inline with a citation marker.
**Decision:** Deferred to BACKLOG. python-docx has no footnote API at all; they live in `word/footnotes.xml` and would need raw XML parsing plus splicing references back into paragraph positions. No sample file available exercises them, so the work would be built and tested against nothing.

## 2026-09-02: Soft delete, and the two UNIQUE constraints nobody budgeted for (Session 10)
**Context:** `deleted_at TEXT` on `matters` and `files`, NULL meaning live. Straightforward until the existing constraints are considered.

    files:   UNIQUE (matter_id, content_sha256)
    matters: name TEXT NOT NULL UNIQUE

A soft-deleted row still occupies its uniqueness slot, so a lawyer who deletes a file and re-uploads it hits `DuplicateFileInMatter` for a file they cannot see, and a deleted matter blocks its own name forever.

**Decision: resolve them asymmetrically, because the levels differ.**

*Re-uploading a soft-deleted file RESTORES it.* The intent is unambiguous — they want that file in the matter — and reporting a "duplicate" for an invisible file is baffling. The flash says what happened rather than what the system did: "This file was previously deleted and has been restored to the matter."

*Reusing a deleted matter's name REFUSES, with a pointer to Deleted items.* Auto-restoring an entire matter is too much action for too small a gesture; creating a matter and restoring one are different intentions.

**Collections and files on disk are preserved through the soft-delete window.** Restoring is meant to be free — re-ingesting a 500-page scan to undo a mis-click would not be a recovery window, it would be a punishment.

**Consequences:** every existing read had to become live-only (`deleted_at IS NULL`), including the `LEFT JOIN` that computes `file_count`, or a deleted file would keep inflating its matter's count. `get_matter_any` exists so result pages can render "this matter has been deleted" instead of a 404 for work the lawyer still owns.

## 2026-09-02: The migration must survive running on every request
**Context:** `init_db()` is called by `connect()`, which runs on essentially every request. SQLite has no `ADD COLUMN IF NOT EXISTS`.
**Decision:** Check `PRAGMA table_info` before altering. A migration that raises the second time it runs would brick the application on its second request — a sharper version of the Session 6a rule that a migration must never turn a working install into a dead one.

`ADD COLUMN` is metadata-only in SQLite, so there is no table rewrite and every existing row reads NULL, which is exactly "not deleted". Verified by connecting four times in a row.

## 2026-09-02: Permanent deletion runs in the background, not on a startup budget
**Context:** Expired items need removing, including their Chroma collections, which is the slow part.
**Decision:** A daemon thread started after the server is already listening, reusing the pattern from `updater.start_background_check()`. Startup cost is zero rather than merely small.

Bounded at 25 items per launch so a large backlog clears over several launches instead of one long pass, and wrapped so a failure cannot touch startup. If the vector store will not open, the rows are left alone rather than dropped — deleting the manifest while the collections survive would orphan them permanently, the same reasoning as the Session 6a backfill.

## 2026-09-02: The model picker is a native select with search layered on top
**Context:** 425 models on OpenRouter, so a picker needs search. The brief specified a CDN library.
**Decision:** Custom, ~80 lines of vanilla JS, and the submitted control is a real server-rendered `<select name="model">` containing every option.

**A CDN library was not a close call.** This application is offline-capable by design — bundled embedding model, bundled Tesseract and Poppler, verified in Session 3 running with every outbound HTTP route blackholed. A dropdown fetched from a CDN would silently never initialise on a disconnected laptop, which is precisely the machine this tool was built for. Bundling a library instead would mean a `static/` directory that does not exist, vendored assets and PyInstaller data entries — more moving parts than the widget.

**Progressive enhancement is the structural lesson from Session 8** applied before the fact. When one stray newline killed that inline script, every JS-driven control on the form died with it. Here, if the script throws, the lawyer still has a long but fully working native dropdown, keyboard- and screen-reader-operable for free. The search box is `hidden` in the markup and revealed by the script, so a dead script leaves no orphaned control rather than an input that does nothing.

Search matches every space-separated term against id, name and provider, so "opus 4.7" and "anthropic opus" both work. The currently selected option is never hidden by a filter — hiding a selected `<option>` makes the control display nothing.

## 2026-09-02: Pricing tiers derived from the catalogue, not chosen
**Decision:** `$` ≤ $2.25, `$$` ≤ $17.50, `$$$` above, in USD per 1M tokens (prompt + completion).

Those are p50 and p90 of the actual distribution across all 425 models, measured rather than guessed: `$` is the cheaper half of the catalogue and `$$$` the most expensive tenth. For orientation: MiMo Pro $1.30, Sonnet 5 $12.00, Sonnet 4.6 $18.00, Opus 4.7 $30.00, and o1-pro at $750 to show what the tail looks like.

If OpenRouter's catalogue shifts materially these should be re-derived rather than nudged — the point is that they describe a real distribution.

**Model ids use dots, not dashes.** `anthropic/claude-opus-4-7` does not exist and 404s; nor does `claude-sonnet-4-7` in any form. Checked against the live list. The third recommended slot is `anthropic/claude-sonnet-5` — newer and cheaper than the 4.6 alternative.

## 2026-09-02: A per-run model override, as a thread-local
**Context:** Every task path resolves its model through `pipeline._config()`. Threading a parameter through eight call sites would be a wide change for a narrow feature.
**Decision:** A thread-local override applied at the single point `_config()` is read. `None` means "use MODEL from the environment", which is byte-for-byte v1.0.4 behaviour — so a lawyer who never touches the picker gets exactly what they had.

Exhaustive mode is unaffected: it pins `EXHAUSTIVE_MODEL` explicitly and never consults the override. The requested model is carried into the run state so the result page can state both, and the audit record carries `model_requested`, `model_used` and a separate `model_coerced` boolean — a later auditor should not have to reconstruct intent by comparing two strings.

## 2026-09-02: Key handling — masked, tested, and never logged at any length
**Decision:** Keys are tested against their live service before being saved, using the same endpoints as the Session 5 first-run wizard, and written through a single `.env` rewrite that also sets `os.environ` explicitly — necessary because python-dotenv does not override variables that are already set, so rewriting the file alone would leave the old key live for the session.

**The mask hides the length as well as the value.** A fixed number of dots, not one per character: the length of an API key narrows the search space, and this string is rendered on a page that may end up in a screenshot in a support thread. Asserted by a test that masks two keys of different lengths and requires the results to be the same size.

Nothing key-shaped reaches a log. The audit entry for a key change records only which service and that the test passed.

**Key changes never interrupt a running task**, and this needed no machinery: `LLMClient` reads the environment once at construction, and each task constructs its own. A task in flight finishes on the key it started with. A multi-batch exhaustive run holds one client for its whole life, so it completes on one key throughout — swapping mid-run would make half a result unattributable.

## 2026-09-03: Cost is measured from the provider, never computed (Session 11)
**Context:** The brief assumed cost would be calculated as `tokens x price`, with the price coming from Session 10's cached model catalogue, and asked what to do when a price changes between cache refreshes.
**Decision:** Check the API before designing around it. OpenRouter returns what it actually charged.

Verified live before any code was written: sending `extra_body={"usage": {"include": True}}` makes the response carry

    "cost": 7.6e-07,
    "cost_details": {"upstream_inference_cost": 7.6e-07,
                     "upstream_inference_prompt_cost": 4.4e-07,
                     "upstream_inference_completions_cost": 3.2e-07}

So the recorded figure is the billed figure. There is no price table to keep current, no cache to invalidate, and no way for a stale price to make a billing record quietly wrong.

A computed number would also have been wrong for three reasons a price table cannot see: prompt caching bills cached tokens differently, OpenRouter may route a request to a different upstream at a different price, and the catalogue may simply be out of date. Each of those produces a figure that is wrong *silently*, which for something a lawyer passes on to a client is the worst available failure mode.

When a response carries no `cost` — an unusual provider, a future API change — the row records NULL and the log shows "Unknown", with token counts still stored. A blank that says so beats a total that is quietly short.

## 2026-09-03: The accumulator lives in the client, not at the call sites
**Context:** Three places construct an `LLMClient`: `pipeline._ask_model`, `exhaustive.run_exhaustive`, and `discovery`. Hooking cost capture at each of them is the obvious approach.
**Decision:** Put it inside `LLMClient.complete()`, accumulating into a thread-local scope opened once per run.

The invariant is what matters: a hook inside the client counts a new call site automatically, whereas a hook at each call site has to be remembered — and the failure mode of forgetting is a billing figure that is silently too low.

That is not hypothetical here. **`discovery` makes TWO model calls per run** — concept extraction at `discovery.py:337` and case notes at `:608`. A per-call-site hook applied to the obvious one would have under-counted Suggest Relevant Cases by roughly half, indefinitely, with no symptom anywhere.

Multiple calls in one run sum rather than double-count, because the scope is opened once by the caller and not once per call. Exhaustive runs open theirs inside the worker thread, since the accumulator is thread-local and the run executes off the request thread.

Every path closes in a `finally`, so a run that dies before reaching the model still records $0.00 with status `failed`. A lawyer asking "why did that task disappear" needs to find the attempt rather than silence.

## 2026-09-03: A pricing bug shipped in v1.0.5, in both directions
**Context:** Session 8 wrote `exhaustive.MODEL_PRICING` with three models and `DEFAULT_PRICING = (5.00, 25.00)` — Opus rates. Session 10 then made all 425 OpenRouter models selectable and did not revisit it.
**Decision:** Source estimates from `model_registry`, which covers the whole catalogue, and keep the three-model table only as an offline last resort.

Measured after the fix:

| model | v1.0.6 | v1.0.5 said |
|---|---|---|
| `mistralai/mistral-nemo` | $0.02 / $0.03 | $5.00 / $25.00 |
| `anthropic/claude-opus-4.7` | $5.00 / $25.00 | correct |
| `openai/o1-pro` | $150 / $600 | $5.00 / $25.00 |

Note the direction. The obvious half of the bug quoted cheap models about 250x too high. The dangerous half quoted the most expensive models **30x too low** — a lawyer would have approved a run at a fraction of its real price. Recorded cost is now measured, so this affects only the pre-run estimate, but that estimate is exactly where a lawyer decides whether to spend the money.

The unknown-model fallback stays deliberately high rather than average: an estimate that is too high makes someone hesitate, whereas one that is too low spends money they did not agree to.

`model_registry` now also keeps prompt and completion prices separately, rather than only the summed figure it used for tier badges, so the estimator can price the two token streams properly instead of halving a total.

## 2026-09-03: matter_name is denormalised onto every cost row
**Context:** Session 10's purge hard-deletes matters 30 days after soft deletion. A cost row referencing one by id alone becomes unreadable the moment that happens.
**Decision:** `matter_id` is a plain integer, deliberately NOT a foreign key, and the matter's name is copied onto the row at write time.

A real foreign key would force one of two wrong outcomes: block the purge, or cascade the billing history away with the matter. Both are unacceptable for an accounting record — the money was spent whatever became of the matter afterwards.

The log therefore renders three states: a live matter by name, a soft-deleted one as "Cresthaven (deleted)", and a purged one as "Cresthaven (removed)" from the stored copy. Verified end to end by soft-deleting and then hard-deleting a matter with costs against it and confirming the rows stayed readable by name.

## 2026-09-03: CanLII is excluded, and the log says so
**Decision:** Only OpenRouter spending is recorded. CanLII is a separate account with separate billing, and folding the two together would misstate both. The cost log states this on the page rather than leaving a lawyer to infer it from an absence.

## 2026-09-03: Backfill recovers one row, and that was known in advance
**Context:** Session 8 recorded exhaustive-run costs into `audit.jsonl` before `task_costs` existed.
**Decision:** Replay them once, marker-guarded, streaming the file line by line.

The honest yield was measured before the code was written: of 87 audit entries, exactly 2 carry `cost_usd`, and one is the zeroed run from before the tally bug was fixed mid-Session-8. So the backfill recovers **one** record. It is worth having — a real historical run is worth preserving and the mechanism is reusable — but nobody should expect it to populate a history.

Rows are tagged `source="backfill"` so a later reader can distinguish a reconstructed figure from one measured at the time. Re-running inserts nothing, guarded by `run_id`.

Audit log size is a non-issue at this scale (79 KB for 87 entries), but the parse streams anyway, since the file grows without bound over a firm's life.
