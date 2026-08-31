from __future__ import annotations

import datetime as dt
import re

from . import citations

# --------------------------------------------------------------------------
# Code-owned pleading safety machinery (SoW Sections 1.4.3 and 4.6).
#
# Everything in this module is owned by the code, not by any task template, so
# none of it can be weakened or removed by editing a YAML file or by the
# Phase 3 prompt curator: the DRAFT banner, the cover note, the limitation
# check, and the required-input gates.
# --------------------------------------------------------------------------

# The four canonical pleading types. These strings are the single source of
# truth: the draft_pleading.yaml `pleading_type` options and `variants` keys
# must match them exactly (enforced at template load by `check_template`).
SOC = "Statement of Claim (Superior Court of Justice)"
SOD = "Statement of Defence (Superior Court of Justice)"
CLAIM_7A = "Plaintiff's Claim — Form 7A (Small Claims Court)"
DEFENCE_9A = "Defence — Form 9A (Small Claims Court)"

PLEADING_TYPES = [SOC, SOD, CLAIM_7A, DEFENCE_9A]
PLAINTIFF_TYPES = {SOC, CLAIM_7A}
DEFENDANT_TYPES = {SOD, DEFENCE_9A}

# Short codes for the CLI (so the user need not retype the full label).
PLEADING_TYPE_BY_CODE = {
    "soc": SOC,
    "sod": SOD,
    "claim7a": CLAIM_7A,
    "defence9a": DEFENCE_9A,
}


# Repeated at the head AND foot of every pleading output (web banner + CLI).
DRAFT_BANNER = "DRAFT — NOT FOR FILING OR SERVICE — NOT REVIEWED BY COUNSEL"

# Cover note per SoW Section 1.4.3 (a)-(d), as a numbered list.
COVER_NOTE = """\
**This document is a DRAFT produced by an automated tool. Before it is used in
any way, note the following:**

1. **Not reviewed by counsel.** This draft has not been reviewed by a lawyer.
   It may contain errors, omissions, or misstatements and must be reviewed by
   counsel before any use.
2. **Limitation periods and deadlines remain your responsibility.** The tool
   does not calculate or guarantee limitation periods or statutory deadlines.
   Confirming that every applicable limitation period and deadline is met
   remains your responsibility.
3. **Pleadings carry admission consequences.** Pleadings can contain admissions
   — including deemed admissions — and other consequences that require a
   lawyer's judgment. Do not rely on this draft's admissions or denials without
   lawyer review.
4. **Do not file or serve without lawyer review.** This document must not be
   filed with any court or served on any party without review by a lawyer. The
   tool provides no filing or serving function and the formatting is
   intentionally not filing-ready."""


# The two gap markers the draft_pleading template instructs the model to emit
# wherever the matter documents do not support a required element or fact. The
# marker TEXT is template-owned (it is drafting guidance), but RECOGNISING one
# is code-owned: Day-4d exports highlight every marker so a reviewer cannot skim
# past an unfilled gap in a Word or PDF document that may be forwarded onward.
# Non-greedy to the first closing bracket, DOTALL because a marker can wrap
# across lines in the model's output.
REQUIRED_MARKER = re.compile(
    r"\[(?:ELEMENTS|ADDITIONAL MATERIAL|AUTHORITY) REQUIRED.*?\]"
    # Phase 2b: the citation-verification markers are highlighted by the same
    # machinery. A [REMOVED — ...] marker in an exported Word or PDF document
    # is MORE important for a reviewer to notice than a gap marker, not less:
    # it records that the tool deleted a case the draft relied on.
    "|" + citations.VERIFICATION_MARKER_PATTERN,
    re.DOTALL,
)


def split_required_markers(text: str) -> list[tuple[str, bool]]:
    """Split prose into (fragment, is_marker) runs so a renderer can style the
    markers distinctly. Returns [(text, False)] when there are none."""
    out: list[tuple[str, bool]] = []
    pos = 0
    for m in REQUIRED_MARKER.finditer(text or ""):
        if m.start() > pos:
            out.append((text[pos : m.start()], False))
        out.append((m.group(0), True))
        pos = m.end()
    if pos < len(text or ""):
        out.append((text[pos:], False))
    return out or [(text or "", False)]


def role_for(pleading_type: str | None) -> str:
    """Derive party role from the pleading type (the 1-to-1 mapping the SoW's
    separate party_role field would otherwise duplicate)."""
    return "Defendant" if pleading_type in DEFENDANT_TYPES else "Plaintiff"


# --------------------------------------------------------------------------
# Required-input gate (SoW Section 4.6 step 1; Section 2.2 inputs)
# --------------------------------------------------------------------------
def validate_pleading_inputs(structured_inputs: dict) -> list[str]:
    """Return human-readable errors for pleading-specific required inputs.

    Conditional on pleading_type, so it lives here rather than in the generic
    declarative `required` flag: plaintiff pleadings need claim particulars;
    defendant pleadings need the opposing-pleading affirmation."""
    errors: list[str] = []
    pt = structured_inputs.get("pleading_type")
    if pt not in PLEADING_TYPES:
        errors.append("Select a pleading type.")
        return errors
    if pt in PLAINTIFF_TYPES and not structured_inputs.get("claim_particulars"):
        errors.append(
            "Plaintiff pleadings require claim particulars (the causes of action "
            "being advanced and the relief sought)."
        )
    if pt in DEFENDANT_TYPES and not structured_inputs.get(
        "opposing_pleading_confirmed"
    ):
        errors.append(
            "To draft a defence, upload the opposing party's pleading and tick "
            "the confirmation that the uploaded PDF is that pleading."
        )
    return errors


# --------------------------------------------------------------------------
# Non-blocking pleading-hallmarks heuristic (defendant pleadings)
# --------------------------------------------------------------------------
_HALLMARKS = [
    "statement of claim",
    "plaintiff's claim",
    "plaintiff",
    "defendant",
    "to the defendant",
    "you are hereby",
    "superior court of justice",
    "small claims court",
    "court file no",
    "claim no",
]


def has_pleading_hallmarks(texts: list[str]) -> bool:
    """True if the uploaded text looks like a pleading (>=2 distinct markers).
    Used only to raise a non-blocking warning, never to block."""
    low = "\n".join(texts).lower()
    return sum(1 for m in _HALLMARKS if m in low) >= 2


# --------------------------------------------------------------------------
# Limitation check (SoW Section 4.6.1) — deliberately false-positive-friendly.
# A false positive costs the user one checkbox; a false negative could let a
# real limitation issue slip past, so we err heavily toward tripping.
# --------------------------------------------------------------------------
LIMITATION_YEARS = 2  # Ontario basic limitation period (Limitations Act, 2002)

_LIMITATION_LEXICON = [
    r"limitation period",
    r"limitations act",
    r"statute[- ]barred",
    r"time[- ]barred",
    r"out of time",
    r"ultimate limitation",
    r"discoverab",        # discoverability / discoverable
    r"limitation defence",
    r"two[- ]year limitation",
    r"\blimitation\b",
    r"\bexpired\b",
    # Broadened in Day 3.5 (fix A) to catch contractual / procedural time bars
    # the original lexicon missed (e.g. a "120-day deadline" in an email chain).
    # Intentionally permissive — see ARCHITECTURE.md "Day 3.5: limitation scanner".
    r"\bdeadline\b",
    r"\b\d+[\s-]?(?:day|days|week|weeks|month|months)\b",  # "120-day", "30 days"
    r"within \d+\s*(?:day|days|week|weeks|month|months|year|years)",
    r"served within",
    r"notice period",
    r"\blaches\b",
    r"prescription",
    r"prescribed period",
    r"intervening period",
    r"evidentiary",       # "gap in the evidentiary record" and the like
]

_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def scan_for_limitation(texts: list[str]) -> list[str]:
    """Return a list of human-readable limitation signals; empty means clear.

    Each signal names the exact thing detected so the reviewing lawyer can
    actually perform the limitation analysis they are about to affirm, rather
    than tick a generic box. Two independent kinds of signal, either of which
    trips:
      1. a term from the limitation lexicon appears in the text, or
      2. the oldest 4-digit year in the text is > LIMITATION_YEARS before now.

    Callers should pass everything that could carry a time bar — both the
    matter document chunks AND the user's typed claim particulars.
    """
    joined = "\n".join(t for t in texts if t)
    low = joined.lower()
    signals: list[str] = []
    seen: set[str] = set()

    for pat in _LIMITATION_LEXICON:
        for m in re.finditer(pat, low):
            term = m.group(0).strip()
            if term and term not in seen:
                seen.add(term)
                signals.append(
                    f'the matter text mentions "{term}" - review whether a '
                    f"limitation period or deadline applies"
                )

    years = [int(y) for y in _YEAR_RE.findall(joined)]
    if years:
        current = dt.date.today().year
        oldest = min(years)
        if oldest <= current - LIMITATION_YEARS:
            signals.append(
                f"the matter references the year {oldest}, more than "
                f"{LIMITATION_YEARS} years before {current} - a limitation "
                f"period may already have run"
            )
    return signals


# --------------------------------------------------------------------------
# Startup consistency check (called by prompts.load_templates)
# --------------------------------------------------------------------------
def check_template(template) -> None:
    """Fail loudly at startup if draft_pleading.yaml drifts from the canonical
    pleading types defined here."""
    pt_field = next((f for f in template.inputs if f.name == "pleading_type"), None)
    if pt_field is None or pt_field.options != PLEADING_TYPES:
        raise ValueError(
            "draft_pleading.yaml `pleading_type` options must equal "
            f"{PLEADING_TYPES}; got {pt_field.options if pt_field else None}"
        )
    if not template.variants or set(template.variants) != set(PLEADING_TYPES):
        raise ValueError(
            "draft_pleading.yaml `variants` keys must equal the four pleading "
            f"types; got {sorted(template.variants or [])}"
        )
