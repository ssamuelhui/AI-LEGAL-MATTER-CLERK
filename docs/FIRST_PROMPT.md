# First message to paste into Claude Code

Once you have:
- VS Code open in the `matter-clerk` folder
- Claude Code authenticated
- All the skeleton files in place (including `docs/SoW.docx`)
- A few test PDFs ready in `data/test_matter/`

Open the Claude Code panel (Cmd+Esc or Ctrl+Esc), then paste the following as your first message:

---

```
Read these files in order before responding:
1. CLAUDE.md
2. docs/SoW.docx (the full specification)
3. config/sources.yaml
4. config/required_docs.yaml

After reading, confirm you understand:
(a) the project objective
(b) the phased approach in SoW Section 8
(c) the citation-verification discipline in SoW Section 4.4
(d) the court hierarchy in SoW Section 4.3.1

Then we're going to begin Phase 1 Day 1, per SoW Section 8: a command-line script that takes one PDF and one question, embeds the PDF into a local Qdrant vector database, retrieves the most relevant chunks, sends them to MiMo Pro via OpenRouter, and prints a cited answer.

Scope rules for Day 1:
- Just the CLI. No web UI yet.
- One PDF at a time. No matter concept yet.
- No CanLII retrieval yet. No procedure tracker. No feedback loop.
- The "citation" for Day 1 is the source filename and the page number the answer came from. Full case-citation verification comes in Phase 2.
- The LLM call goes through a class called `LLMClient` with one method `complete(messages) -> response`. This is the abstraction layer so we can swap providers later without rewriting calling code.

Before you write any code, propose:
1. The file layout under `src/matter_clerk/` for the Day-1 deliverable
2. The libraries you intend to use (with versions where it matters)
3. The CLI invocation form you'll provide (i.e., what flags it takes)
4. Any dependencies on the user (e.g., "I need you to have Docker running and Qdrant started")

Wait for my approval before writing any files. After I approve, write the code, then tell me exactly how to run it and what to expect.
```

---

## What to do next

When Claude Code responds, it will probably:

1. Summarize its understanding of the four points (a)-(d).
2. Propose a file layout (something like `cli.py`, `ingest.py`, `retrieve.py`, `llm_client.py`, `cite.py`).
3. List its library picks (likely: `pypdf`, `qdrant-client`, `sentence-transformers`, `openai`, `python-dotenv`, `click` or `typer`).
4. Tell you to make sure Docker and Qdrant are running.
5. Stop and wait for approval.

Read what it proposes carefully. Push back on anything that feels off. Two specific things to watch for:

- **Does it use `pypdf` or something else for PDF parsing?** `pypdf` is fine for native PDFs. If you'll have scanned PDFs in the test matter, ask if it's planning to handle them — for Day 1 we can defer OCR to a later step.

- **Does it propose using the openai Python SDK pointed at OpenRouter?** That's the standard pattern. If it proposes anything more exotic, ask why.

When you're ready, approve with something like:

```
Layout looks good. Go ahead and write Day 1. Use pypdf for now; we'll add OCR for scanned PDFs in a later step.
```

## After Day 1 works

Verify by running it against a test PDF. If you get a cited answer, Day 1 is done. Then start Day 2 with:

```
Day 1 works. Commit what you have to git with a clear message, then start Day 2 per SoW Section 8: wrap the CLI in a Flask web interface with file upload. Same rules — propose the layout first, wait for my approval.
```

Each subsequent day or phase follows the same pattern. Propose → approve → build → verify → commit → next.
