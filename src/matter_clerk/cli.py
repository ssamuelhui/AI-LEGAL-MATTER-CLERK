from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .citation import Citation
from .embed import embed, embedding_dimension
from .ingest import chunk_pages, extract_pdf_pages
from .llm import LLMClient
from .prompts import DAY1_SYSTEM_PROMPT, build_user_message
from .vectorstore import (
    collection_exists,
    connect,
    default_collection_name,
    file_hash,
    recreate_collection,
    search,
    upsert_chunks,
)

log = logging.getLogger("matter_clerk")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="matter-clerk",
        description="Day-1 CLI: one PDF + one question -> cited answer.",
    )
    p.add_argument("--pdf", type=Path, required=True, help="Path to a native-text PDF.")
    p.add_argument("--question", type=str, required=True, help="The user's question.")
    p.add_argument("--top-k", type=int, default=8, help="Retrieved chunks (default: 8).")
    p.add_argument(
        "--collection",
        type=str,
        default=None,
        help="Qdrant collection name (default: derived from PDF hash).",
    )
    p.add_argument(
        "--reindex",
        action="store_true",
        help="Re-ingest even if the collection already exists.",
    )
    return p.parse_args(argv)


def _precheck_qdrant(client) -> None:
    try:
        client.get_collections()
    except Exception as e:
        sys.exit(
            f"ERROR: Qdrant unreachable. Run `docker compose up -d` and retry. ({e})"
        )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_dotenv()
    args = parse_args(argv)

    if not args.pdf.is_file():
        sys.exit(f"ERROR: PDF not found: {args.pdf}")

    host = os.environ.get("QDRANT_HOST", "localhost")
    port = int(os.environ.get("QDRANT_PORT", "6333"))
    embed_model = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
    model = os.environ.get("MODEL", "xiaomi/mimo-v2.5-pro")

    client = connect(host, port)
    _precheck_qdrant(client)

    coll = args.collection or default_collection_name(args.pdf)
    needs_index = args.reindex or not collection_exists(client, coll)

    if needs_index:
        log.info(f"Ingesting {args.pdf.name} ...")
        pages, skipped = extract_pdf_pages(args.pdf)
        if skipped:
            log.warning(
                f"WARN: skipped {len(skipped)} page(s) with no extractable text "
                f"(likely scanned; OCR is deferred to ING-2): {skipped}"
            )
        if not pages:
            sys.exit("ERROR: No extractable text on any page of the PDF.")
        chunks = chunk_pages(pages, source=args.pdf.name)
        log.info(f"Embedding {len(chunks)} chunks with {embed_model} ...")
        vectors = embed([c.text for c in chunks], model_name=embed_model)
        recreate_collection(client, coll, dim=embedding_dimension(embed_model))
        upsert_chunks(client, coll, chunks, vectors)
        log.info(f"Indexed into collection: {coll}")
    else:
        log.info(f"Reusing existing collection: {coll}")

    log.info("Retrieving relevant chunks ...")
    query_vec = embed([args.question], model_name=embed_model)[0]
    hits = search(client, coll, query_vec, args.top_k)
    retrieved = [
        {
            "source": h.payload["source"],
            "page": h.payload["page"],
            "text": h.payload["text"],
        }
        for h in hits
    ]
    if not retrieved:
        sys.exit("ERROR: No chunks retrieved. Try --reindex or a different question.")

    log.info("Asking the model ...")
    llm = LLMClient(model=model)
    answer = llm.complete(
        [
            {"role": "system", "content": DAY1_SYSTEM_PROMPT},
            {"role": "user", "content": build_user_message(args.question, retrieved)},
        ]
    )

    # Build Citation objects for the [CITATIONS] block.
    citations: list[Citation] = []
    seen: set[tuple[str, str]] = set()
    for c in retrieved:
        page_label = f"p.{c['page']}"
        key = (c["source"], page_label)
        if key in seen:
            continue
        seen.add(key)
        snippet = c["text"].strip().replace("\n", " ")
        if len(snippet) > 160:
            snippet = snippet[:160] + "..."
        citations.append(
            Citation(
                source=c["source"], page_or_paragraph=page_label, text_snippet=snippet
            )
        )

    print("[ANSWER]")
    print(answer.strip())
    print()
    print("[CITATIONS]")
    for cit in citations:
        print(f'- {cit.inline()}  "{cit.text_snippet}"')
    print()
    print("[RUN METADATA]")
    print(
        f"model: {model}   retrieval: top-{args.top_k} semantic ({embed_model})"
    )
    print(f"timestamp: {dt.datetime.now(dt.timezone.utc).isoformat()}")
    print(f"pdf_sha256: {file_hash(args.pdf)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
