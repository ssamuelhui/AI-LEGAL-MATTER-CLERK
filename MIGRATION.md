# Migration: Qdrant → ChromaDB (Phase 3)

**Applies to:** any installation running Phase 2b or earlier.
**Date:** 2026-08-30

Matter Clerk no longer uses Qdrant, and therefore no longer needs Docker. The
vector store is now ChromaDB running **embedded** — an ordinary folder on disk
(`data/chroma/`), opened in-process. There is nothing to start before using the
tool and nothing to keep running alongside it.

---

## What you need to do

### 1. Update dependencies

```
pip install -e .
```

This installs `chromadb` and drops `qdrant-client`.

### 2. Re-ingest your matter files

**This is the one manual step, and it is required.** Vectors do not transfer
between Qdrant and ChromaDB. Your matters, files, filenames, and history are all
in `matter_clerk.db` and are untouched — but the *searchable index* has to be
rebuilt from the documents.

Until a file is re-ingested, querying the matter that contains it will return no
passages from that file.

**Web UI:** open each matter and re-upload its files. A file whose content
matches one already in the matter is rejected as a duplicate, so **delete the
old rows first**, or add the files to a freshly created matter.

**CLI:**

```
matter-clerk matter add "<matter name>" path/to/file.pdf
```

Re-ingestion cost is the same as first ingestion: embedding runs locally on CPU,
roughly a few seconds per document, and OCR (if the PDF needs it) is the slow
part. A 7-file matter takes a few minutes.

### 3. Stop and remove Qdrant

Once you have verified your matters return results:

```
docker compose -f docs/legacy/docker-compose.yml down
```

You may then delete the `qdrant_storage/` folder. **Keep it until you are
satisfied** — see Rollback below.

### 4. Update your `.env`

`QDRANT_HOST` and `QDRANT_PORT` are no longer read and can be deleted. No new
variable is required. Optionally set `CHROMA_DB_PATH` to move the store
somewhere other than `data/chroma/`:

```
# CHROMA_DB_PATH=D:\MatterClerkIndex
```

---

## What changed, and what didn't

**Changed:** where vectors are stored, and how the tool reaches them. That's it.

**Unchanged, deliberately:**

- Chunking — same page-boundary chunker, same token windows
- Embeddings — same `BAAI/bge-small-en-v1.5`, computed locally, same vectors
- Retrieval semantics — same cosine similarity, same top-k, same scatter-gather
  across a matter, same per-file grouping for Compare Clauses
- Collection naming — still `day1-<sha16>` and `m<matter_id>-<sha16>`
- Citations — `[FILENAME p.N]`, `p.5 (OCR)`, and email locators are stored and
  returned verbatim
- All safety machinery — the limitation gate, DRAFT banners, matter-only mode,
  and Phase 2b citation verification are untouched

Answers to the same question over the same documents should be equivalent. They
will not be *byte-identical*, because the LLM is not deterministic — but the
retrieved passages and their ranking are.

---

## Data locations

| What | Where | Migrated automatically? |
|---|---|---|
| Matters, files, ingest status | `matter_clerk.db` | Yes — untouched |
| Uploaded source documents | `data/matters/` | Yes — untouched |
| **Vector index** | `data/chroma/` (was Qdrant, port 6333) | **No — re-ingest** |
| Audit log | `logs/audit.jsonl` | Yes — untouched |

`data/chroma/` is a `chroma.sqlite3` file plus one directory per collection's
HNSW index. It is gitignored: it contains matter text and is confidential.

---

## Rollback

The rollback path is git, not a configuration switch:

```
git revert <migration commit>
pip install -e .
docker compose -f docs/legacy/docker-compose.yml up -d
```

Because the old `qdrant_storage/` volume is left in place by this migration and
nothing in the new code touches it, reverting restores a working Phase-2b tool
with its index **already populated** — no re-ingestion needed to go back. This
is why you should not delete `qdrant_storage/` until you are confident.

---

## Known constraint: one process at a time

Qdrant was a server, so the web app and the CLI could both use it
simultaneously. An embedded store is owned by a single process.

- **Fine:** the web app under normal use. It is multi-threaded and ChromaDB
  serialises access internally.
- **Not supported:** running a CLI command against the same store while the web
  app is running. Stop the web app first.

For the single-lawyer-single-laptop deployment this tool targets, this is not a
practical limitation, but it is a real difference from Phase 2b.

---

## Troubleshooting

**"The document index could not be opened."**
The data folder is missing or not writable. Check `data/chroma/` (or whatever
`CHROMA_DB_PATH` points at) exists and the account running the tool can write
to it. This banner replaces the old "Qdrant is not reachable" message — if you
still see the old one, the update didn't apply.

**A matter returns no passages.**
Its files have not been re-ingested. See step 2.

**"Collection ... does not exist."**
Same cause: the database row survived the migration but the index did not.
Re-ingest that file.
