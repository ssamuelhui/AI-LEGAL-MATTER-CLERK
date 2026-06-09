from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import pipeline

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


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_dotenv()
    args = parse_args(argv)

    if not args.pdf.is_file():
        sys.exit(f"ERROR: PDF not found: {args.pdf}")

    try:
        result = pipeline.run_query(
            pdf_path=args.pdf,
            source_name=args.pdf.name,
            question=args.question,
            top_k=args.top_k,
            reindex=args.reindex,
            collection=args.collection,
        )
    except pipeline.QdrantUnreachable as e:
        sys.exit(
            f"ERROR: Qdrant unreachable. Run `docker compose up -d` and retry. ({e})"
        )
    except pipeline.PdfHasNoText as e:
        sys.exit(f"ERROR: {e}")

    if result.ocr_pages:
        log.info(
            f"INFO: recovered {len(result.ocr_pages)} page(s) via OCR: {result.ocr_pages}"
        )
    if result.unreadable_pages:
        log.warning(
            f"WARN: {len(result.unreadable_pages)} page(s) unreadable after OCR "
            f"(no extractable text and not blank): {result.unreadable_pages}"
        )

    print("[ANSWER]")
    print(result.answer)
    print()
    print("[CITATIONS]")
    for cit in result.citations:
        print(f'- {cit.inline()}  "{cit.text_snippet}"')
    print()
    print("[RUN METADATA]")
    print(
        f"model: {result.model}   retrieval: top-{result.top_k} semantic ({result.embed_model})"
    )
    print(f"timestamp: {result.timestamp}")
    print(f"pdf_sha256: {result.pdf_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
