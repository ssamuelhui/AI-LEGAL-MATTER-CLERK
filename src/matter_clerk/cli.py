from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import pipeline
from .prompts import DEFAULT_TASK, missing_required_inputs, ordered_templates

log = logging.getLogger("matter_clerk")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    task_ids = [t.id for t in ordered_templates()]
    p = argparse.ArgumentParser(
        prog="matter-clerk",
        description="One PDF + one task -> cited output (matter-only).",
    )
    p.add_argument("--pdf", type=Path, required=True, help="Path to a PDF.")
    p.add_argument(
        "--task",
        type=str,
        default=DEFAULT_TASK,
        choices=task_ids,
        help=f"Task template to run (default: {DEFAULT_TASK}).",
    )
    p.add_argument(
        "--question",
        type=str,
        default=None,
        help="Task input: the question / issue / focus / instruction.",
    )
    p.add_argument(
        "--recipient",
        type=str,
        default=None,
        help="Task input for draft_correspondence: who the letter is to.",
    )
    p.add_argument(
        "--categories",
        type=str,
        default=None,
        help="Task input for find_entities: comma-separated category list.",
    )
    p.add_argument(
        "--top-k",
        type=int,
        default=None,
        help="Retrieved chunks (default: the task template's own value).",
    )
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


def _collect_inputs(args: argparse.Namespace) -> dict:
    """Map CLI flags onto task-input names. Unmatched keys are filtered out by
    the caller against the chosen template's declared inputs."""
    inputs: dict = {}
    if args.question:
        inputs["question"] = args.question
    if args.recipient:
        inputs["recipient"] = args.recipient
    if args.categories:
        inputs["categories"] = [
            c.strip() for c in args.categories.split(",") if c.strip()
        ]
    return inputs


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_dotenv()
    args = parse_args(argv)

    if not args.pdf.is_file():
        sys.exit(f"ERROR: PDF not found: {args.pdf}")

    template = pipeline.get_template(args.task)
    structured_inputs = _collect_inputs(args)
    field_names = {f.name for f in template.inputs}
    structured_inputs = {k: v for k, v in structured_inputs.items() if k in field_names}

    missing = missing_required_inputs(template, structured_inputs)
    if missing:
        sys.exit(
            f"ERROR: task '{args.task}' requires: {', '.join(missing)}. "
            "Supply via --question / --recipient."
        )

    try:
        result = pipeline.run_query(
            pdf_path=args.pdf,
            source_name=args.pdf.name,
            task=args.task,
            structured_inputs=structured_inputs,
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

    print(f"[TASK] {template.label} (matter-only)")
    print()
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
