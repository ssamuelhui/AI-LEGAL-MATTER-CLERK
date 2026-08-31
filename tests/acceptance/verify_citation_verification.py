"""Verification for Phase-2b citation verification.

    python tests/acceptance/verify_citation_verification.py          # offline
    python tests/acceptance/verify_citation_verification.py --live   # + CanLII

Offline covers extraction, the outcome model, answer rewriting, prompt-mode
switching (including that matter-only output is byte-identical to Phase 1), and
export marker/disclaimer plumbing. --live checks real and fabricated citations
against the CanLII API.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from pypdf import PdfReader  # noqa: E402

from matter_clerk import citations, export, verification  # noqa: E402
from matter_clerk.citation import Citation  # noqa: E402
from matter_clerk.citations import CaseCitation  # noqa: E402
from matter_clerk.export.payload import ExportPayload  # noqa: E402
from matter_clerk.prompts import (  # noqa: E402
    AUTHORITY_MODE_ON,
    build_system_prompt,
    get_template,
)
from matter_clerk.verification import Outcome, VerificationReport, VerificationResult  # noqa: E402

_passed = 0
_failed: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  [ok]   {label}")
    else:
        _failed.append(f"{label}{(' - ' + detail) if detail else ''}")
        print(f"  [FAIL] {label}{(' - ' + detail) if detail else ''}")


# --------------------------------------------------------------------------
SAMPLE = """The corporation's duty to repair is settled: Metropolitan Toronto \
Condominium Corporation No. 590 v. Registered Owners, 2020 ONCA 471, 149 O.R. \
(3d) 481, is directly on point.
Good faith was addressed in Wastech Services Ltd. v. Greater Vancouver, 2021 \
SCC 7.
Older authority: Frontenac Condominium Corp. No. 1 v. Macciocchi, 1975 CanLII \
499 (ON CA).
A fabricated case: Smith v. Jones, 2027 ONCA 999, holds otherwise.
An old-style cite: [2005] 2 S.C.R. 601.
Facts remain grounded [Condo Bylaw 6.pdf p.3]. Not citations: the 2020 Revenue \
15 report, or 2019 Budget 4."""


def verify_extraction() -> None:
    print("\nCitation extraction")
    found = citations.extract_citations(SAMPLE)
    got = [c.full_citation for c in found]
    check("neutral citation extracted", "2020 ONCA 471" in got, str(got))
    check("SCC citation extracted", "2021 SCC 7" in got)
    check("CanLII-series citation extracted", "1975 CanLII 499 (ON CA)" in got)
    check("fabricated citation extracted (so it can be checked)",
          "2027 ONCA 999" in got)
    check("reporter-only citation recognised", "[2005] 2 S.C.R. 601" in got)

    # Over-extraction deletes text from a legal memo, so this is not cosmetic.
    check("prose is NOT extracted as a citation",
          not any("Revenue" in g or "Budget" in g for g in got), str(got))
    check("matter-document citations are not case citations",
          not any("Bylaw" in g for g in got))

    by = {c.full_citation: c for c in found}
    check("parallel reporter tail is swallowed into the span",
          by["2020 ONCA 471"].raw_text == "2020 ONCA 471, 149 O.R. (3d) 481",
          repr(by["2020 ONCA 471"].raw_text))
    check("case name captured",
          by["2020 ONCA 471"].case_name
          == "Metropolitan Toronto Condominium Corporation No. 590 v. "
             "Registered Owners",
          repr(by["2020 ONCA 471"].case_name))
    check("leading connector stripped from the name",
          by["2021 SCC 7"].case_name
          == "Wastech Services Ltd. v. Greater Vancouver",
          repr(by["2021 SCC 7"].case_name))
    check("context sentence captured whole",
          by["2027 ONCA 999"].context_sentence.startswith("A fabricated case:"),
          repr(by["2027 ONCA 999"].context_sentence))
    check("caseId is lowercase (CanLII rejects uppercase)",
          by["2020 ONCA 471"].case_id == "2020onca471")
    check("reporter-only citation is marked unsupported",
          not by["[2005] 2 S.C.R. 601"].supported)

    # The no-space form a sloppy model emits.
    nospace = citations.extract_citations("See 2020ONSC4583 for the point.")
    check("no-space citation normalised",
          nospace and nospace[0].full_citation == "2020 ONSC 4583",
          str([c.full_citation for c in nospace]))


def verify_normalisation() -> None:
    """Regression guard for a real bug: a normaliser that only stripped a
    TRAILING parenthetical failed to match CanLII's "2021 SCC 7 (CanLII),
    [2021] 1 SCR 32" against the model's "2021 SCC 7", reporting a genuine
    Supreme Court authority as fabricated and stripping it from the memo."""
    print("\nCitation normalisation")
    n = citations.normalise_citation
    check("CanLII's parallel-citation suffix does not defeat the match",
          n("2021 SCC 7") == n("2021 SCC 7 (CanLII), [2021] 1 SCR 32"))
    check("(CanLII) suffix does not defeat the match",
          n("2020 ONCA 471") == n("2020 ONCA 471 (CanLII)"))
    check("a model-side parallel tail does not defeat the match",
          n("2020 ONCA 471, 149 O.R. (3d) 481") == n("2020 ONCA 471 (CanLII)"))
    check("different cases still differ",
          n("2019 ONSC 4484") != n("2020 ONCA 471"))


def verify_name_matching() -> None:
    print("\nCase-name matching")
    m = citations.names_match
    check("wholly different names mismatch",
          not m("Smith v. Jones",
                "Metropolitan Toronto Condominium Corporation No. 590 v. "
                "Registered Owners"))
    check("corporate suffix differences still match",
          m("Wastech Services Ltd. v. Greater Vancouver",
            "Wastech Services Inc. v. Greater Vancouver Sewerage"))
    check("abbreviated party still matches",
          m("Orr v. MTCC 1056",
            "Orr v. Metropolitan Toronto Condominium Corporation No. 1056"))
    check("no name given is never a mismatch", m(None, "Anything v. Anything"))
    check("reversed parties on appeal still match",
          m("Jones v. Smith", "Smith v. Jones"))
    check("numbered company matches on its number",
          m("1420041 Ontario Inc. v. King West",
            "1420041 Ontario Inc. v. 1 King West Inc."))


def verify_outcomes_and_rewrite() -> None:
    print("\nOutcomes and answer rewriting")
    check("only NOT_FOUND strips", Outcome.NOT_FOUND.strips)
    for o in (Outcome.VERIFIED, Outcome.NAME_MISMATCH, Outcome.UNVERIFIABLE,
              Outcome.UNSUPPORTED):
        check(f"{o.value} does NOT strip", not o.strips)

    text = "Good: 2020 ONCA 471. Bad: 2027 ONCA 999. Old: [2005] 2 S.C.R. 601."
    found = citations.extract_citations(text)
    by = {c.full_citation: c for c in found}
    report = VerificationReport(results=[
        VerificationResult(by["2020 ONCA 471"], Outcome.VERIFIED),
        VerificationResult(by["2027 ONCA 999"], Outcome.NOT_FOUND),
        VerificationResult(by["[2005] 2 S.C.R. 601"], Outcome.UNSUPPORTED),
    ])
    out = verification.apply_to_answer(text, report)
    check("verified citation retained and marked",
          "2020 ONCA 471 [verified in CanLII]" in out, out)
    check("fabricated citation REMOVED from the text",
          "2027 ONCA 999" not in out.split("[REMOVED")[0], out)
    check("removal leaves a transparent marker",
          "[REMOVED — citation not verified: 2027 ONCA 999]" in out, out)
    check("unsupported citation RETAINED (stripping it would be a false claim)",
          "[2005] 2 S.C.R. 601 [UNVERIFIED" in out, out)
    check("rewriting right-to-left kept every span aligned",
          out.count("[verified in CanLII]") == 1
          and out.count("[REMOVED") == 1 and out.count("[UNVERIFIED") == 1)

    audit = verification.build_audit_payload(report)
    check("audit records every extracted citation",
          len(audit["citations_extracted"]) == 3, str(audit["citations_extracted"]))
    check("audit records the stripped citation",
          audit["citations_stripped"][0]["citation"] == "2027 ONCA 999")
    check("audit carries the EXTRACTION CONTEXT for the stripped citation",
          "Bad:" in audit["citations_stripped"][0]["extraction_context"],
          str(audit["citations_stripped"][0]))
    check("audit stores no case content",
          not any("held" in str(v).lower() for v in audit.values()))


def verify_prompt_modes() -> None:
    """Matter-only must be byte-identical to Phase 1, and authority mode must
    REPLACE the prohibition rather than sit alongside it."""
    print("\nPrompt modes")
    for task, extra in (("draft_memo", {"question": "q"}),
                        ("draft_pleading",
                         {"pleading_type":
                          "Statement of Claim (Superior Court of Justice)"})):
        t = get_template(task)
        mo = build_system_prompt(t, dict(extra))
        am = build_system_prompt(t, dict(extra, authority_mode=AUTHORITY_MODE_ON))
        check(f"{task}: matter-only keeps the prohibition",
              "must not cite any case, statute" in mo)
        check(f"{task}: matter-only has no authority-mode text",
              "AUTHORITY MODE" not in mo)
        check(f"{task}: authority mode LIFTS the prohibition",
              "must not cite any case, statute" not in am)
        check(f"{task}: authority mode adds the instruction",
              "Every citation you give WILL be checked" in am)
        check(f"{task}: authority mode keeps fact-citation discipline",
              "[SOURCE: ...]" in am)
        check(f"{task}: escape hatch offered", "[AUTHORITY REQUIRED" in am)
        check(f"{task}: no contradictory instruction survives",
              not ("MATTER-ONLY MODE" in am and "AUTHORITY MODE" in am),
              "both modes present in one prompt")

    # Authority mode must be unavailable to every other task.
    t = get_template("summarize")
    check("a non-authority task ignores the flag entirely",
          build_system_prompt(t, {}) ==
          build_system_prompt(t, {"authority_mode": AUTHORITY_MODE_ON}))

    # Pleading DRAFT machinery is code-owned and independent of authority mode.
    t = get_template("draft_pleading")
    am = build_system_prompt(
        t, {"pleading_type": "Statement of Claim (Superior Court of Justice)",
            "authority_mode": AUTHORITY_MODE_ON})
    check("pleading gap markers survive authority mode",
          "[ADDITIONAL MATERIAL REQUIRED" in am)
    check("ELEMENTS REQUIRED marker survives authority mode",
          "[ELEMENTS REQUIRED" in am)


def verify_marker_recognition() -> None:
    print("\nMarker recognition (export highlighting)")
    from matter_clerk import pleadings
    text = ("A [ELEMENTS REQUIRED — do x] and [REMOVED — citation not "
            "verified: 2027 ONCA 999] and [UNVERIFIED — citation format "
            "cannot be checked against CanLII: [2005] 2 S.C.R. 601] end")
    segs = pleadings.split_required_markers(text)
    markers = [t for t, is_m in segs if is_m]
    check("pleading gap marker recognised",
          any("ELEMENTS REQUIRED" in m for m in markers))
    check("removal marker recognised", any("REMOVED" in m for m in markers))
    check("nested-bracket marker captured WHOLE",
          any(m.endswith("2 S.C.R. 601]") for m in markers), str(markers))


def _payload(authority: bool, answer: str) -> ExportPayload:
    return ExportPayload(
        task="draft_memo", task_label="Draft Memo", answer_markdown=answer,
        citations=[Citation(source="Condo Bylaw 6.pdf", page_or_paragraph="p.3",
                            text_snippet="The corporation shall repair.")],
        matter_name="Imperial Plaza Test", source_files=["Condo Bylaw 6.pdf"],
        authority_mode=authority,
        verification_summary="2 verified · 1 removed as unverified" if authority else "",
        model="test-model", timestamp="2026-08-27T12:00:00+00:00",
    )


def verify_exports() -> None:
    print("\nExport integration")
    answer = ("### Analysis\n\nThe duty is settled, 2020 ONCA 471 "
              "[verified in CanLII]. A fabricated one was "
              "[REMOVED — citation not verified: 2027 ONCA 999].\n")
    p_auth = _payload(True, answer)
    p_plain = _payload(False, answer)

    check("authority mode turns on marker highlighting for a memo",
          p_auth.highlight_markers)
    check("matter-only memo still does not highlight",
          not p_plain.highlight_markers)

    docx = export.generate(p_auth, "docx")[0]
    with zipfile.ZipFile(io.BytesIO(docx)) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    check("Word: disclaimer sentence 1 present",
          "confirm the cases exist" in xml)
    check("Word: disclaimer sentence 2 present (the load-bearing one)",
          "do NOT" in xml and "held what is stated" in xml)
    check("Word: verification summary present", "2 verified" in xml)
    check("Word: REMOVED marker preserved", "2027 ONCA 999" in xml)
    check("Word: verified marker preserved", "verified in CanLII" in xml)
    check("Word: highlight applied to a marker", "highlight" in xml.lower())

    pdf = export.generate(p_auth, "pdf")[0]
    text = "\n".join(
        (pg.extract_text() or "") for pg in PdfReader(io.BytesIO(pdf)).pages
    )
    check("PDF: disclaimer present", "confirm the cases exist" in text)
    check("PDF: REMOVED marker preserved", "2027 ONCA 999" in text)
    check("PDF: no missing-glyph box for the tick (marker is WinAnsi-safe)",
          "✓" not in text)

    docx_plain = export.generate(p_plain, "docx")[0]
    with zipfile.ZipFile(io.BytesIO(docx_plain)) as z:
        xml_plain = z.read("word/document.xml").decode("utf-8")
    check("matter-only export carries NO authority disclaimer",
          "confirm the cases exist" not in xml_plain)


def verify_web_rendering() -> None:
    print("\nWeb rendering")
    from matter_clerk.web import decorate_verification_markers, render_markdown
    html = decorate_verification_markers(render_markdown(
        "Good 2020 ONCA 471 [verified in CanLII], bad "
        "[REMOVED — citation not verified: 2027 ONCA 999], old "
        "[UNVERIFIED — citation format cannot be checked against CanLII: "
        "[2005] 2 S.C.R. 601]."))
    check("verified badge rendered with a tick",
          'class="cite-ok"' in html and "&#10003;" in html)
    check("removal badge rendered", 'class="cite-bad"' in html)
    check("nested-bracket marker styled whole",
          "2 S.C.R. 601]</span>" in html, html[-200:])
    check("no raw script/attr leakage from the sanitiser",
          "<script" not in html)


# --------------------------------------------------------------------------
def verify_live() -> None:
    """Real CanLII lookups. Costs ~6 calls."""
    print("\nLIVE CanLII verification")
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    text = ("Real: MTCC No. 590 v. Registered Owners, 2020 ONCA 471. "
            "Real SCC: Wastech Services Ltd. v. Greater Vancouver, 2021 SCC 7. "
            "Real older: Frontenac Condominium Corp. No. 1 v. Macciocchi, "
            "1975 CanLII 499 (ON CA). "
            "Fabricated: Smith v. Jones, 2027 ONCA 999. "
            "Wrong name on a real cite: Anderson v. Baker, 2019 ONSC 4484. "
            "Unverifiable format: [2005] 2 S.C.R. 601.")
    found = citations.extract_citations(text)
    report = verification.verify_citations(found)
    by = {
        r.citation.full_citation: r.outcome for r in report.distinct_results()
    }
    check("real ONCA citation verifies",
          by.get("2020 ONCA 471") is Outcome.VERIFIED, str(by.get("2020 ONCA 471")))
    check("real SCC citation verifies (parallel-citation regression)",
          by.get("2021 SCC 7") is Outcome.VERIFIED, str(by.get("2021 SCC 7")))
    check("real CanLII-series citation verifies",
          by.get("1975 CanLII 499 (ON CA)") is Outcome.VERIFIED,
          str(by.get("1975 CanLII 499 (ON CA)")))
    check("fabricated citation is NOT_FOUND",
          by.get("2027 ONCA 999") is Outcome.NOT_FOUND,
          str(by.get("2027 ONCA 999")))
    check("real citation with an invented name is NAME_MISMATCH",
          by.get("2019 ONSC 4484") is Outcome.NAME_MISMATCH,
          str(by.get("2019 ONSC 4484")))
    check("reporter-only citation is UNSUPPORTED",
          by.get("[2005] 2 S.C.R. 601") is Outcome.UNSUPPORTED)
    check("verification completed without a network failure",
          not report.incomplete)
    print(f"         summary: {report.summary_line()}")
    print(f"         CanLII calls: {report.calls_made}")

    out = verification.apply_to_answer(text, report)
    check("the fabricated citation is gone from the rewritten draft",
          "2027 ONCA 999" not in out.replace(
              "[REMOVED — citation not verified: 2027 ONCA 999]", ""))
    check("the real SCC citation survived the rewrite", "2021 SCC 7" in out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="also verify against the real CanLII API (~6 calls)")
    args = ap.parse_args()

    print("Phase-2b citation verification")
    verify_extraction()
    verify_normalisation()
    verify_name_matching()
    verify_outcomes_and_rewrite()
    verify_prompt_modes()
    verify_marker_recognition()
    verify_exports()
    verify_web_rendering()
    if args.live:
        verify_live()

    print(f"\n{_passed} passed, {len(_failed)} failed")
    for f in _failed:
        print(f"  FAIL: {f}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
