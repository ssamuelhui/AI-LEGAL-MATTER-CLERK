# Claude Code Project Context — Matter Clerk

This file orients Claude Code at the start of each session. Read this before touching any other file.

## What this project is

A document-grounded AI tool for Canadian legal work. Specification: `docs/SoW.docx`.

## The single most important rule

**Every output the system produces must carry citations to verifiable sources, and every citation must be verified before output is returned.** Hallucinated case citations are the gravest failure this system can produce. Test CIT-2 in Section 5 of the SoW deliberately attempts to make the system produce a fabricated citation. The system must refuse.

## How we work together

**One phase at a time.** The SoW Section 8 lays out 5 phases. Do not jump ahead. Before starting a phase, propose the file layout and the libraries you intend to use, then wait for user approval. After completing each phase, demonstrate that the phase's deliverable works before starting the next.

**Architectural decisions go in `docs/ARCHITECTURE.md`** at the time of the decision, not later. The user reads this log to understand why the code looks the way it does.

**Scope discipline.** The SoW is explicit about what's in and out of scope. If you're tempted to add a feature that's not in the current phase, ask first. "Not in this phase" is a complete answer.

**Anti-overfitting rule for prompt templates.** The gold-set test queries in `tests/gold_set/` are how the user verifies that prompt changes don't regress quality. Do not read those files while writing prompt templates. They are the test set; using them as training material defeats their purpose.

## Project conventions

- **Python 3.11+.** Use `pyproject.toml`. Use type hints. Use `pydantic` for structured data.
- **LLM access goes through one interface** — a single `LLMClient` class with a `complete(messages) -> response` method. Provider swap is a one-file change. Default provider is MiMo Pro via OpenRouter per the SoW; the user may swap to Claude or another model for testing.
- **Never call external APIs directly from prompt logic.** Retrieval (CanLII, Ontario Courts, rule sources) is its own module. The LLM never invents URLs.
- **Citations are first-class objects.** Define a `Citation` dataclass with `source`, `page_or_paragraph`, `text_snippet`, `url`, and `court_rank` (for CanLII). Every claim of fact in every output points to a `Citation`.
- **Retrieval URLs live in `config/sources.yaml`.** Code reads from there. Do not hardcode URLs in Python.
- **Required documents per (forum, proceeding type, deadline) live in `config/required_docs.yaml`.** Code reads from there. Do not hardcode in Python.
- **No prompt cites a case the system has not retrieved.** The flow is always: retrieve from CanLII → inject retrieved text into context → instruct model to cite only from injected text → verify every citation against the retrieved source before output.

## Court hierarchy (SoW Section 4.3.1)

When the system retrieves case law, results are ranked in this order:
1. Supreme Court of Canada (SCC) — binding everywhere
2. Ontario Court of Appeal (ONCA) — binding in Ontario
3. Ontario Superior Court of Justice (ONSC) — persuasive within Ontario
4. All other Canadian courts — persuasive only, labelled as such

Ontario is the default jurisdiction. Out-of-jurisdiction authority is presented only when no Ontario authority is on point, and is always labelled "persuasive only — no Ontario authority located on this point."

## Procedure tracker rule sources (SoW Section 4.5)

Four primary rule sources, plus Notices to the Profession from the relevant court:
1. **Rules of Civil Procedure** (Ontario Superior Court)
2. **Rules of the Small Claims Court** (Ontario)
3. **Residential Tenancies Act + LTB Rules** (Landlord and Tenant Board)
4. **Court of Appeal rules and practice directions** (Ontario Court of Appeal)

Federal Court is a secondary forum if needed.

Rule text is fetched at run time, not from cached training data. Cached content older than 24 hours triggers a fresh fetch.

## What's where

```
src/matter_clerk/         the Python package (to be created in Phase 1)
prompts/templates/        task templates (one file per task type)
config/sources.yaml       URLs for rule sources and Notices to the Profession
config/required_docs.yaml catalog of required documents per deadline
data/test_matter/         the 50-200 page test matter (user-supplied)
data/adversarial_matter/  the adversarial test matter (user-supplied)
tests/gold_set/           gold-set queries (user-supplied; KEEP OUT OF MODEL CONTEXT during template work)
tests/acceptance/         tests implementing SoW Section 5 acceptance criteria
docs/SoW.docx             the binding spec
docs/ARCHITECTURE.md      decisions log
```

## Current phase

**Phase 0 complete:** skeleton, configs, dependencies.

**Phase 1 starting:** see SoW Section 8 and the first-message prompt from the user.

The user will tell you when to start each phase. Do not start unprompted.
