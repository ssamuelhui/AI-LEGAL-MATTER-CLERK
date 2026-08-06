from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import matters, pipeline, pleadings
from .prompts import DEFAULT_TASK, missing_required_inputs, ordered_templates
from .vectorstore import file_hash

log = logging.getLogger("matter_clerk")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    task_ids = [t.id for t in ordered_templates()]
    p = argparse.ArgumentParser(
        prog="matter-clerk",
        description="One PDF + one task -> cited output (matter-only).",
    )
    p.add_argument(
        "--pdf", type=Path, required=True, help="Path to a PDF or .eml file."
    )
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
        "--pleading-type",
        type=str,
        default=None,
        choices=list(pleadings.PLEADING_TYPE_BY_CODE),
        help="Task input for draft_pleading: which of the four pleading types.",
    )
    p.add_argument(
        "--claim-particulars",
        type=str,
        default=None,
        help="Task input for draft_pleading (plaintiff): causes of action and relief.",
    )
    p.add_argument(
        "--opposing-pleading-confirmed",
        action="store_true",
        help="draft_pleading (defendant): affirm the uploaded PDF is the opposing pleading.",
    )
    p.add_argument(
        "--limitation-confirmed",
        action="store_true",
        help="draft_pleading: affirm a limitation analysis has been completed.",
    )
    p.add_argument(
        "--detail-level",
        type=str,
        default=None,
        choices=["Concise", "Detailed"],
        help="Task input for timeline: Detailed captures every dated event "
        "(and, in matter mode, retrieves more per file). Default: Concise.",
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
    if args.detail_level:
        inputs["detail_level"] = args.detail_level
    if args.recipient:
        inputs["recipient"] = args.recipient
    if args.categories:
        inputs["categories"] = [
            c.strip() for c in args.categories.split(",") if c.strip()
        ]
    if args.pleading_type:
        inputs["pleading_type"] = pleadings.PLEADING_TYPE_BY_CODE[args.pleading_type]
    if args.claim_particulars:
        inputs["claim_particulars"] = args.claim_particulars
    if args.opposing_pleading_confirmed:
        inputs["opposing_pleading_confirmed"] = True
    if args.limitation_confirmed:
        inputs["limitation_confirmed"] = True
    return inputs


# --------------------------------------------------------------------------
# `matter-clerk matter <verb>` — matter management (Day 4a).
#
# Structurally separate from the flat `--pdf/--task` CLI: main() routes here
# before the flat parser runs, so the existing single-file workflow is wholly
# unaffected. Shares the ingest primitive (`pipeline.ingest_file`) with the web
# upload path, so the two cannot drift.
# --------------------------------------------------------------------------
def _add_file_to_matter(conn, matter: matters.Matter, file_path: Path) -> matters.MatterFile:
    """Copy `file_path` into the matter store, ingest it, and record the result.

    Order matters: insert the manifest row FIRST (so a duplicate is rejected by
    content hash before any copy/ingest work), then copy, then ingest. A failed
    ingest leaves a 'failed' row with the error rather than a silent gap.
    """
    if not file_path.is_file():
        raise matters.MatterError(f"file not found: {file_path}")
    suffix = file_path.suffix.lower()
    if suffix not in (".pdf", ".eml"):
        raise matters.MatterError(
            f"unsupported file type: {suffix} (use .pdf or .eml)"
        )

    sha = file_hash(file_path)
    file_type = "eml" if suffix == ".eml" else "pdf"
    coll = matters.collection_name(matter.id, sha)
    stored = matters.stored_path_for(matter.id, sha, suffix)

    # Raises DuplicateFileInMatter (specific message) if this content is already
    # in the matter — before we copy or ingest anything.
    row = matters.add_file_pending(
        conn, matter.id, file_path.name, file_type, sha, coll, str(stored)
    )

    stored.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(file_path, stored)
    try:
        pipeline.ingest_file(stored, file_path.name, collection=coll)
        matters.mark_file_ingested(conn, row.id)
    except Exception as e:
        matters.mark_file_failed(conn, row.id, str(e))
        raise matters.MatterError(f"ingest failed for {file_path.name}: {e}")
    return matters.get_file(conn, row.id)


def _matter_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="matter-clerk matter",
        description="Manage matters (Day 4a: create, list, add files, show).",
    )
    sub = p.add_subparsers(dest="verb", required=True)

    pc = sub.add_parser("create", help="Create a new matter.")
    pc.add_argument("name", help="Matter name (must be unique).")
    pc.add_argument("--description", default=None, help="Optional description.")

    sub.add_parser("list", help="List all matters, most recently active first.")

    pa = sub.add_parser("add", help="Ingest a PDF or .eml file into a matter.")
    pa.add_argument("name", help="Target matter name.")
    pa.add_argument("file", type=Path, help="Path to a .pdf or .eml file.")

    ps = sub.add_parser("show", help="Show a matter's metadata and its files.")
    ps.add_argument("name", help="Matter name.")

    args = p.parse_args(argv)

    conn = matters.connect()
    try:
        if args.verb == "create":
            m = matters.create_matter(conn, args.name, args.description)
            print(f"Created matter #{m.id}: {m.name}")
            if m.description:
                print(f"  {m.description}")

        elif args.verb == "list":
            ms = matters.list_matters(conn)
            if not ms:
                print("No matters yet. Create one with: "
                      "matter-clerk matter create <name>")
                return 0
            for m in ms:
                last = m.last_queried_at or "never queried"
                print(
                    f"#{m.id}  {m.name}  "
                    f"({m.file_count} file(s), last queried: {last})"
                )

        elif args.verb == "add":
            m = matters.get_matter_by_name(conn, args.name)
            if m is None:
                sys.exit(
                    f"ERROR: no matter named {args.name!r}. "
                    f"Create it with: matter-clerk matter create {args.name!r}"
                )
            f = _add_file_to_matter(conn, m, args.file)
            print(
                f"Added {f.filename} to matter {m.name!r} "
                f"(status: {f.ingest_status}, collection: {f.collection})"
            )

        elif args.verb == "show":
            m = matters.get_matter_by_name(conn, args.name)
            if m is None:
                sys.exit(f"ERROR: no matter named {args.name!r}.")
            print(f"Matter #{m.id}: {m.name}")
            if m.description:
                print(f"  {m.description}")
            print(f"  created: {m.created_at}")
            print(f"  last queried: {m.last_queried_at or 'never'}")
            files = matters.list_files(conn, m.id)
            if not files:
                print("  (no files yet - add one with: "
                      f"matter-clerk matter add {m.name!r} <file>)")
            else:
                print(f"  {len(files)} file(s):")
                for f in files:
                    ingested = f.ingested_at or "-"
                    line = (f"    [{f.id}] {f.filename}  ({f.file_type}, "
                            f"{f.ingest_status}, ingested: {ingested})")
                    if f.ingest_status == "failed" and f.ingest_error:
                        line += f"\n        error: {f.ingest_error}"
                    print(line)
    except matters.MatterError as e:
        sys.exit(f"ERROR: {e}")
    finally:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # qdrant-client logs every request via httpx at INFO ("HTTP Request: GET
    # ... 200 OK"); quiet it to WARNING so successful calls are silent but
    # failures still surface. Matches web.main().
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_dotenv()

    raw = sys.argv[1:] if argv is None else argv
    if raw and raw[0] == "matter":
        return _matter_main(raw[1:])

    args = parse_args(argv)

    if not args.pdf.is_file():
        sys.exit(f"ERROR: file not found: {args.pdf}")

    if args.pdf.suffix.lower() not in (".pdf", ".eml"):
        sys.exit(f"ERROR: unsupported file type: {args.pdf.suffix} (use .pdf or .eml)")

    template = pipeline.get_template(args.task)
    structured_inputs = _collect_inputs(args)
    field_names = {f.name for f in template.inputs}
    structured_inputs = {k: v for k, v in structured_inputs.items() if k in field_names}

    missing = missing_required_inputs(template, structured_inputs)
    if missing:
        sys.exit(
            f"ERROR: task '{args.task}' requires: {', '.join(missing)}. "
            "See `matter-clerk --help` for the matching flags."
        )

    if args.task == "draft_pleading":
        pleading_errors = pleadings.validate_pleading_inputs(structured_inputs)
        if pleading_errors:
            sys.exit("ERROR: " + " ".join(pleading_errors))

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
    except pipeline.LimitationReviewRequired as e:
        msg = ["LIMITATION ANALYSIS REQUIRED — pleading not drafted.", ""]
        msg.append("A potential limitation-period issue was detected:")
        msg.extend(f"  - {s}" for s in e.signals)
        msg.append("")
        msg.append(
            "Review whether any limitation period applies, then re-run with "
            "--limitation-confirmed to proceed."
        )
        sys.exit("\n".join(msg))

    if result.ocr_pages:
        log.info(
            f"INFO: recovered {len(result.ocr_pages)} page(s) via OCR: {result.ocr_pages}"
        )
    if result.unreadable_pages:
        log.warning(
            f"WARN: {len(result.unreadable_pages)} page(s) unreadable after OCR "
            f"(no extractable text and not blank): {result.unreadable_pages}"
        )

    if result.email_metadata:
        m = result.email_metadata
        print("[EMAIL]")
        print(f"from: {m.sender_name}" + (f" <{m.sender_email}>" if m.sender_email else ""))
        if m.to:
            print(f"to: {m.to}")
        if m.cc:
            print(f"cc: {m.cc}")
        print(f"date: {m.date_iso or '(unknown)'}")
        if m.subject:
            print(f"subject: {m.subject}")
        if m.message_id:
            print(f"message-id: {m.message_id}")
        print()

    if result.attachment_warnings:
        log.warning(
            f"WARN: {len(result.attachment_warnings)} attachment(s) detected and "
            f"NOT processed (body only was indexed): {result.attachment_warnings}"
        )

    for w in result.pleading_warnings:
        log.warning(f"WARN: {w}")

    if args.task == "draft_pleading":
        print(pleadings.DRAFT_BANNER)
        print()
        print("[COVER NOTE]")
        print(pleadings.COVER_NOTE)
        print()
        print(f"[PLEADING DRAFT] {template.label} (matter-only)")
        print(result.answer)
        print()
        print(pleadings.DRAFT_BANNER)
    else:
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
