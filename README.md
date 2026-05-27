# Matter Clerk

A private, document-grounded AI tool for Canadian legal work. Built per the Statement of Work in `docs/SoW.docx`.

## What this is

The Matter Clerk takes a folder of documents associated with a single legal matter (pleadings, contracts, correspondence, exhibits, transcripts, notes), and answers questions, drafts memos, builds timelines, tracks procedural deadlines, and produces other deliverables — all with citations back to source documents and verified CanLII authority.

The tool is built on Xiaomi MiMo Pro via OpenRouter, with retrieval over a local Qdrant vector database, and integrates current-time fetches from CanLII and the Ontario courts' websites.

## Project status

This repository is being built in phases per Section 8 of the SoW. Current phase: **Phase 0 — environment setup.**

| Phase | Status | Deliverable |
|---|---|---|
| 0 — Environment | in progress | Repo skeleton, dependencies, API keys |
| 1 — Walking skeleton | not started | CLI: one PDF → one question → cited answer |
| 1 — Web UI | not started | Flask app with file upload |
| 1 — Task templates | not started | Summarize, Timeline, Find Facts, Draft Memo |
| 1 — Matter concept | not started | Multiple documents per matter |
| 2 — CanLII + Procedure Tracker | not started | Court-hierarchy ranking, four rule sources |
| 3 — Feedback loop | not started | Capture, refinement, prompt curator |
| 4 — Acceptance testing | not started | Section 5 tests |

## Repository layout

```
matter-clerk/
├── README.md
├── docs/
│   ├── SoW.docx                  Statement of Work (binding spec)
│   └── ARCHITECTURE.md           Architecture decisions log
├── src/
│   └── matter_clerk/             Python package (created in Phase 1)
├── prompts/
│   └── templates/                Task templates (created in Phase 1)
├── tests/
│   ├── gold_set/                 Gold-set queries (built by user, kept out of model context during template-writing)
│   └── acceptance/               Tests for Section 5 acceptance criteria
├── config/
│   ├── sources.yaml              Authoritative URLs for rule sources and NTPs
│   └── required_docs.yaml        Catalog of required documents per (forum, proceeding type, deadline)
├── data/
│   ├── test_matter/              Anonymised test matter documents (50-200 pages)
│   └── adversarial_matter/       Adversarial test matter with fabricated facts
├── pyproject.toml
├── docker-compose.yml            Qdrant + any other local services
├── .env.example                  Template for environment variables
└── .gitignore
```

## Quick start (after Phase 1 is built)

```bash
# Set up environment
cp .env.example .env
# Edit .env to add your OPENROUTER_API_KEY

# Start the vector database
docker compose up -d

# Install Python deps
pip install -e .

# Run the CLI (Phase 1 Day 1)
python -m matter_clerk.cli --pdf path/to/document.pdf --question "What is the principal claim?"
```

## Key references

- `docs/SoW.docx` — the binding specification. Read first.
- Section 5 of the SoW — acceptance criteria. The tool is complete when these pass.
- Section 4 of the SoW — autonomous processes. Read before building any retrieval logic.
- Section 4.3.1 — court hierarchy. SCC → ONCA → ONSC → other Canadian courts. Ontario preferred; out-of-jurisdiction authority labelled persuasive only.
- Section 4.5 — Procedure Tracker covers four rule sources: Rules of Civil Procedure, Rules of the Small Claims Court, Residential Tenancies Act + LTB Rules, Court of Appeal rules and practice directions.

## Anti-hallucination discipline

Every citation, in every output, is verified against its source before output is returned. This is non-negotiable per Section 4.4 of the SoW. The Citation Verification step strips any citation that cannot be verified and replaces it with `[citation needed — please verify]`. Test CIT-2 deliberately attempts to make the tool produce a fabricated citation; the build is not accepted if it succeeds.
