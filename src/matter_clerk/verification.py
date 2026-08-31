"""Citation verification against CanLII (Phase 2b).

Takes the citations `citations.extract_citations` found in model output, checks
each against CanLII, and rewrites the answer so the lawyer can see exactly what
survived checking and what did not.

WHAT VERIFICATION MEANS, AND WHAT IT DOES NOT
---------------------------------------------
A verified citation means: **a case with this citation exists in CanLII, and the
case name given is compatible with CanLII's title for it.**

It does NOT mean the case held what the surrounding sentence says it held.
CanLII's API returns metadata only — there is no case text to check a
proposition against, and there never will be through this interface. Every
piece of user-facing language in this feature has to carry that distinction,
because a green tick that a reader interprets as "this claim is correct" is
worse than no tick at all.

HOW IT WORKS
------------
CanLII resolves a case directly from its neutral citation:

    GET /caseBrowse/{lang}/{db}/{caseId}/   caseId = "2020onca471"
    -> 200 with metadata   (the case exists)
    -> 404                 (it does not)

Deterministic, one call per distinct citation, no search and no ranking. Search
is NOT a fallback: `fullText="2020 ONCA 471"` returns cases that CITE that
decision, never the decision itself, so a search-based check would be wrong in
both directions (verified at 2026-08-27).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from . import canlii, citations
from .canlii import CanLIICase, CanLIIClient
from .citations import CaseCitation

log = logging.getLogger("matter_clerk.verification")

# Upper bound on API calls spent verifying one answer. At the ~1.1s effective
# interval this caps verification at roughly 45 seconds. A model that emits more
# distinct citations than this has produced something a lawyer must review line
# by line regardless; the excess are reported as unchecked rather than silently
# blessed or silently dropped.
MAX_VERIFICATIONS_PER_RUN = 40

# The disclaimer shown wherever an authority-mode result is presented — the web
# result page and the head of every Word/PDF export. Code-owned and identical in
# all three, because an exported file can be forwarded to someone who never saw
# the screen it came from.
#
# The two-sentence structure is the point: what verification confirms, then what
# it does not. A tick that a reader takes to mean "this claim is correct" is
# worse than no tick at all, and only the second sentence prevents that reading.
AUTHORITY_DISCLAIMER = (
    "Citations verified against CanLII confirm the cases exist. They do NOT "
    "confirm the cases actually held what is stated. You must read each cited "
    "case yourself before relying on any claim made about it."
)


class Outcome(str, Enum):
    """What checking one citation established.

    Five outcomes rather than a boolean, because "we checked and it is not
    there", "we could not check", and "this format cannot be checked" have
    completely different consequences for the document and must not be
    collapsed into "unverified"."""

    VERIFIED = "verified"
    NAME_MISMATCH = "name_mismatch"
    NOT_FOUND = "not_found"
    UNVERIFIABLE = "unverifiable"
    UNSUPPORTED = "unsupported"

    @property
    def strips(self) -> bool:
        """Whether this outcome removes the citation from the answer.

        ONLY `NOT_FOUND` strips. That is the only outcome where we affirmatively
        established the case does not exist. Stripping on `UNVERIFIABLE` would
        assert fabrication because a network call failed; stripping on
        `UNSUPPORTED` would delete valid Supreme Court citations because of a
        CanLII data-format limitation. Both would put a false statement into a
        legal document."""
        return self is Outcome.NOT_FOUND

    @property
    def is_clean(self) -> bool:
        return self is Outcome.VERIFIED


@dataclass
class VerificationResult:
    citation: CaseCitation
    outcome: Outcome
    canlii_case: CanLIICase | None = None
    error: str | None = None

    @property
    def marker(self) -> str:
        """The text that replaces (or annotates) this citation in the answer."""
        c = self.citation.full_citation
        if self.outcome is Outcome.VERIFIED:
            return f"{self.citation.raw_text} {citations.MARK_VERIFIED}"
        if self.outcome is Outcome.NAME_MISMATCH:
            actual = self.canlii_case.title if self.canlii_case else "another case"
            return (
                f"{self.citation.raw_text} "
                + citations.mark_mismatch(c, self.citation.case_name or "", actual)
            )
        if self.outcome is Outcome.NOT_FOUND:
            return citations.mark_removed(c)
        if self.outcome is Outcome.UNSUPPORTED:
            return f"{self.citation.raw_text} {citations.mark_unsupported(c)}"
        return f"{self.citation.raw_text} {citations.mark_unverifiable(c)}"


@dataclass
class VerificationReport:
    """Everything the result page, the export and the audit log need."""

    results: list[VerificationResult] = field(default_factory=list)
    calls_made: int = 0
    # True when at least one citation could not be checked because CanLII was
    # unreachable — drives the page-level "verification did not complete" banner.
    incomplete: bool = False

    def _of(self, *outcomes: Outcome) -> list[VerificationResult]:
        return [r for r in self.results if r.outcome in outcomes]

    @property
    def verified(self) -> list[VerificationResult]:
        return self._of(Outcome.VERIFIED)

    @property
    def stripped(self) -> list[VerificationResult]:
        return self._of(Outcome.NOT_FOUND)

    @property
    def mismatched(self) -> list[VerificationResult]:
        return self._of(Outcome.NAME_MISMATCH)

    @property
    def unchecked(self) -> list[VerificationResult]:
        return self._of(Outcome.UNVERIFIABLE, Outcome.UNSUPPORTED)

    @property
    def any_citations(self) -> bool:
        return bool(self.results)

    def distinct_results(self) -> list[VerificationResult]:
        """One row per distinct citation, worst outcome first.

        `results` holds one entry per OCCURRENCE so every mention gets its
        marker; the summary table wants one row per case. Ordered so a stripped
        or mismatched citation is the first thing read, not something the lawyer
        has to scroll to find among the verified ones."""
        rank = {
            Outcome.NOT_FOUND: 0,
            Outcome.NAME_MISMATCH: 1,
            Outcome.UNVERIFIABLE: 2,
            Outcome.UNSUPPORTED: 3,
            Outcome.VERIFIED: 4,
        }
        seen: set[str] = set()
        out: list[VerificationResult] = []
        for r in self.results:
            key = citations.normalise_citation(r.citation.full_citation)
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
        out.sort(key=lambda r: rank.get(r.outcome, 9))
        return out

    def summary_line(self) -> str:
        """One-line status for the result page and the export cover."""
        if not self.results:
            return "No case citations were found in this draft."
        bits = [f"{len(self.verified)} verified"]
        if self.mismatched:
            bits.append(f"{len(self.mismatched)} name mismatch")
        if self.stripped:
            bits.append(f"{len(self.stripped)} removed as unverified")
        if self.unchecked:
            bits.append(f"{len(self.unchecked)} could not be checked")
        return " · ".join(bits)


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------
def verify_citations(
    cites: list[CaseCitation], client: CanLIIClient | None = None
) -> VerificationReport:
    """Check each DISTINCT citation against CanLII.

    Serial, not parallel: CanLII permits one concurrent request, so the
    "parallelise within the rate limit" instinct is unavailable here. The shared
    Phase-2a rate gate paces the loop.

    Results are keyed back to every occurrence of each citation, so a case cited
    six times costs one API call and all six occurrences get the same marker."""
    report = VerificationReport()
    if not cites:
        return report

    distinct = citations.unique_citations(cites)
    by_key: dict[str, VerificationResult] = {}

    if client is None:
        try:
            client = CanLIIClient()
        except canlii.CanLIIError as e:
            # No key, or no client. Every citation becomes UNVERIFIABLE — never
            # NOT_FOUND, because nothing was actually checked.
            log.warning(f"CanLII client unavailable for verification: {e}")
            return _all_unverifiable(cites, str(e))

    budget_exhausted = False
    for i, cite in enumerate(distinct):
        key = citations.normalise_citation(cite.full_citation)
        if not cite.supported:
            by_key[key] = VerificationResult(cite, Outcome.UNSUPPORTED)
            continue
        if budget_exhausted or i >= MAX_VERIFICATIONS_PER_RUN:
            by_key[key] = VerificationResult(
                cite, Outcome.UNVERIFIABLE,
                error=f"more than {MAX_VERIFICATIONS_PER_RUN} distinct citations "
                      f"in one draft; this one was not checked",
            )
            report.incomplete = True
            continue

        before = client.calls_made
        try:
            case = client.case_metadata_by_id(cite.database_id, cite.case_id)
        except canlii.CanLIINotFound:
            by_key[key] = VerificationResult(cite, Outcome.NOT_FOUND)
            report.calls_made += client.calls_made - before
            continue
        except (canlii.CanLIIThrottled, canlii.CanLIIBudgetExceeded) as e:
            # Stop spending calls, but keep classifying the rest honestly.
            budget_exhausted = True
            by_key[key] = VerificationResult(cite, Outcome.UNVERIFIABLE, error=str(e))
            report.incomplete = True
            report.calls_made += client.calls_made - before
            continue
        except canlii.CanLIIError as e:
            by_key[key] = VerificationResult(cite, Outcome.UNVERIFIABLE, error=str(e))
            report.incomplete = True
            report.calls_made += client.calls_made - before
            continue
        report.calls_made += client.calls_made - before

        # Exists. Now: does CanLII's citation actually match what was asked for,
        # and is the case name compatible?
        if citations.normalise_citation(case.citation) != key:
            # CanLII resolved the id to a different citation than the one the
            # model wrote. Treat as not found rather than quietly accepting a
            # neighbouring case.
            log.warning(
                f"CanLII resolved {cite.full_citation!r} to {case.citation!r}"
            )
            by_key[key] = VerificationResult(cite, Outcome.NOT_FOUND, canlii_case=case)
            continue
        if not citations.names_match(cite.case_name, case.title):
            by_key[key] = VerificationResult(
                cite, Outcome.NAME_MISMATCH, canlii_case=case
            )
            continue
        by_key[key] = VerificationResult(cite, Outcome.VERIFIED, canlii_case=case)

    # Fan the distinct results back out over every occurrence.
    for cite in cites:
        base = by_key[citations.normalise_citation(cite.full_citation)]
        report.results.append(
            VerificationResult(cite, base.outcome, base.canlii_case, base.error)
        )
    return report


def _all_unverifiable(cites: list[CaseCitation], error: str) -> VerificationReport:
    report = VerificationReport(incomplete=True)
    for c in cites:
        report.results.append(
            VerificationResult(
                c,
                Outcome.UNSUPPORTED if not c.supported else Outcome.UNVERIFIABLE,
                error=error,
            )
        )
    return report


# --------------------------------------------------------------------------
# Rewriting the answer
# --------------------------------------------------------------------------
def apply_to_answer(answer: str, report: VerificationReport) -> str:
    """Rewrite `answer` with each citation marked or stripped.

    Applied right-to-left so earlier spans keep their offsets — rewriting
    left-to-right would invalidate every subsequent span the moment the first
    replacement changed the text length."""
    if not report.results:
        return answer
    ordered = sorted(report.results, key=lambda r: r.citation.span[0], reverse=True)
    out = answer
    for r in ordered:
        start, end = r.citation.span
        out = out[:start] + r.marker + out[end:]
    return out


def build_audit_payload(report: VerificationReport) -> dict:
    """Audit fields for the `citation_verification` event.

    Stripped citations carry their CONTEXT SENTENCE. Without it the log records
    that something was removed but not what it was attached to, which is the
    thing a lawyer reviewing the draft actually needs to know. No case content
    is recorded because CanLII provides none, and no model output beyond the
    sentence around each citation."""
    seen: set[str] = set()
    extracted: list[str] = []
    for r in report.results:
        key = citations.normalise_citation(r.citation.full_citation)
        if key not in seen:
            seen.add(key)
            extracted.append(r.citation.full_citation)

    def distinct(results, with_context: bool = False):
        out, done = [], set()
        for r in results:
            key = citations.normalise_citation(r.citation.full_citation)
            if key in done:
                continue
            done.add(key)
            if with_context:
                out.append(
                    {
                        "citation": r.citation.full_citation,
                        "case_name": r.citation.case_name,
                        "extraction_context": r.citation.context_sentence,
                        "reason": r.error or r.outcome.value,
                    }
                )
            else:
                out.append(r.citation.full_citation)
        return out

    return {
        "citations_extracted": extracted,
        "citations_verified": distinct(report.verified),
        "citations_stripped": distinct(report.stripped, with_context=True),
        "citations_name_mismatched": distinct(report.mismatched, with_context=True),
        "citations_unchecked": distinct(report.unchecked, with_context=True),
        "verification_calls_made": report.calls_made,
        "verification_incomplete": report.incomplete,
    }
