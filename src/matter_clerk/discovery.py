"""Case discovery orchestration (Phase 2a).

This is DISCOVERY, not research. The tool narrows the field of Canadian cases a
lawyer might want to read and hands them a shortlist with live CanLII links.
It never reads a case, never says what one held, and never substitutes for the
lawyer opening the decision.

That distinction is forced on us by the API -- CanLII exposes metadata, not case
text -- but it is also the correct division of labour, so the code leans into it
rather than working around it. Everything downstream of `run_case_discovery` is
built to make the boundary visible: the notes are generated under a preamble
that forbids holding-claims, the funnel counts are surfaced, and the queries
sent to CanLII are recorded verbatim in the audit log.

Pipeline, in order:

  1. Retrieve matter passages locally (embedded store, never leaves the machine).
  2. LLM call 1  -- passages + research direction -> legal CONCEPTS + a set of
     targeted CanLII queries, one per analytical angle.
  3. Scrub the queries. Matter content must not travel to CanLII.
  4. Execute targeted queries; broaden only if the targeted pass comes back
     thin; fall back to browsing the relevant courts only if that is empty too.
  5. Merge, dedupe by citation, hard-filter, rank on cheap signals.
  6. Enrich the top slice with catchwords (one API call each), re-rank.
  7. LLM call 2 -- CONCEPTS + case metadata -> one preliminary note per case.
     Raw matter passages are deliberately NOT in this prompt.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, ValidationError

from . import audit, canlii
from .canlii import CanLIICase, CanLIIClient
from .embed import embed
from .llm import LLMClient
from .matters import MatterFile
from .prompts import (
    build_case_note_prompt,
    build_concept_extraction_prompt,
    get_template,
)
from .vectorstore import connect, search_across_collections

log = logging.getLogger("matter_clerk.discovery")

TASK_ID = "suggest_cases"

# Depth of the local matter retrieval that feeds concept extraction. Generous:
# this costs no API calls and the concepts are only as good as the context.
MATTER_TOP_K = 14

# Query-count envelope. The template asks for one query per supported angle and
# to SKIP angles the matter does not support rather than padding to a quota.
MIN_QUERIES = 3
MAX_QUERIES = 10

# Cases per CanLII search page (API maximum).
SEARCH_PAGE = canlii.MAX_RESULT_COUNT

# Below this many surviving cases, the targeted pass is treated as too thin and
# the broadened pass runs. Approved design: targeted first, broaden if empty.
BROADEN_THRESHOLD = 8
MAX_BROADENED_QUERIES = 3

# Enrichment costs one API call per case, so only a slice of the ranked pool is
# enriched: max_cases plus headroom for the re-rank to reorder within.
ENRICH_HEADROOM = 10
ENRICH_CAP = 30

MAX_CASES_LIMIT = 25
DEFAULT_MAX_CASES = 15
DATE_RANGE_LIMIT = 50
DEFAULT_DATE_RANGE = 10

# --------------------------------------------------------------------------
# Ranking weights (Q4 of the approved proposal).
#
# Court authority is the largest single weight, per SoW 4.3.1 and the approved
# design -- but this is a WEIGHTED SUM, not a lexicographic sort by tier. A
# strict tier ordering guarantees that a stale, off-point SCC case outranks a
# directly-on-point recent ONCA case, which is a worse shortlist than the one it
# replaces. At 0.40 the court dominates near-ties and can be overcome by roughly
# a two-tier gap plus strong relevance evidence.
# --------------------------------------------------------------------------
W_COURT = 0.40
W_JURISDICTION = 0.20
W_ANGLES = 0.15
W_SEARCH_RANK = 0.15
W_RECENCY = 0.10
W_SUBJECT = 0.20  # stage 2 only, added on top

JURISDICTION_CHOICES = ("Ontario", "Federal", "All Canada")

# Ontario is the default jurisdiction per CLAUDE.md / SoW 4.3.1.
DEFAULT_JURISDICTION = "All Canada"


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class CaseDiscoveryError(RuntimeError):
    """User-facing failure; rendered verbatim on the matter page."""


class NoMatterContext(CaseDiscoveryError):
    """The matter has no ingested files, or retrieval returned nothing."""


# --------------------------------------------------------------------------
# LLM-facing models
# --------------------------------------------------------------------------
class GeneratedQuery(BaseModel):
    """One CanLII search string plus why it was asked.

    `rationale` never reaches CanLII -- it exists so the result page can show
    the lawyer the reasoning behind each query and let them judge whether the
    framing was right."""

    angle: str
    query: str
    rationale: str = ""


class MatterConcepts(BaseModel):
    """The de-identified legal shape of the matter.

    This is the ONLY representation of the matter that travels beyond the local
    machine: the CanLII queries are built from it, and it is what the note
    prompt sees in place of raw passages. Everything in it is meant to be
    generic legal vocabulary -- doctrines, statutes, forum, fact pattern in the
    abstract -- with no party names, addresses, unit numbers, or amounts."""

    legal_issues: list[str] = Field(default_factory=list)
    statutes: list[str] = Field(default_factory=list)
    forum: str = ""
    fact_pattern: str = ""
    party_types: list[str] = Field(default_factory=list)
    queries: list[GeneratedQuery] = Field(default_factory=list)

    def concept_terms(self) -> list[str]:
        """Flat term list used for catchword overlap scoring."""
        parts = list(self.legal_issues) + list(self.statutes) + list(self.party_types)
        parts.append(self.fact_pattern)
        parts.append(self.forum)
        return [p for p in parts if p]

    def summary_lines(self) -> list[str]:
        """Human-readable rendering, shown on the result page and given to the
        note prompt in place of the matter passages."""
        out = []
        if self.legal_issues:
            out.append("Legal issues: " + "; ".join(self.legal_issues))
        if self.statutes:
            out.append("Statutes/rules in the documents: " + "; ".join(self.statutes))
        if self.forum:
            out.append("Forum: " + self.forum)
        if self.party_types:
            out.append("Party types: " + ", ".join(self.party_types))
        if self.fact_pattern:
            out.append("Fact pattern: " + self.fact_pattern)
        return out


class CaseNote(BaseModel):
    citation: str
    note: str


# --------------------------------------------------------------------------
# Result models
# --------------------------------------------------------------------------
@dataclass
class RankedCase:
    """One shortlisted case with its score breakdown and preliminary note."""

    case: CanLIICase
    score: float
    breakdown: dict[str, float] = field(default_factory=dict)
    note: str = ""
    # True when the note was assembled deterministically from catchwords
    # because the model did not return a usable one for this case. Surfaced so
    # a reader is never misled about where the sentence came from.
    note_is_fallback: bool = False


@dataclass
class DiscoveryFunnel:
    """Where the cases went. Shown on the result page so the narrowing is
    inspectable rather than a black box."""

    queries_executed: int = 0
    raw_hits: int = 0
    unique_cases: int = 0
    after_filters: int = 0
    enriched: int = 0
    shown: int = 0
    broadened: bool = False
    used_browse_fallback: bool = False


@dataclass
class CaseDiscoveryResult:
    """Deliberately NOT a PipelineResult.

    This task produces no grounded answer, no citations into matter documents,
    and no retrieval top_k -- the three things PipelineResult exists to carry.
    Forcing it into that model would corrupt the shared result page and the
    Day-4d export payload, both of which assume `answer` is legal prose the
    lawyer can rely on. Here there is no such prose by design."""

    matter_id: int
    matter_name: str
    research_direction: str
    jurisdiction: str
    max_cases: int
    date_range_years: int
    concepts: MatterConcepts
    queries: list[GeneratedQuery]
    cases: list[RankedCase]
    funnel: DiscoveryFunnel
    warnings: list[str]
    model: str
    timestamp: str
    canlii_calls: int
    budget_warning: str | None = None

    @property
    def has_out_of_jurisdiction(self) -> bool:
        """True when an Ontario-scoped run surfaced non-Ontario authority --
        drives the SoW 4.3.1 'persuasive only' banner."""
        if self.jurisdiction != "Ontario":
            return False
        return any(
            rc.case.court.jurisdiction not in ("on", "ca", "")
            for rc in self.cases
        )


# --------------------------------------------------------------------------
# Query hygiene
# --------------------------------------------------------------------------
# Matter content must not travel to CanLII. The extraction prompt forbids
# identifiers, but a prompt is an instruction, not a guarantee, so the output is
# also scrubbed in code before any query leaves the machine.
#
# Years are deliberately PRESERVED: "Condominium Act, 1998" and "Residential
# Tenancies Act, 2006" are the statute names, and stripping the year would break
# the single most valuable term in a statutory query. A bare year is not
# identifying; a suite number or a dollar figure is.
_SCRUB_EMAIL = re.compile(r"\S+@\S+")
_SCRUB_URL = re.compile(r"https?://\S+|www\.\S+")
_SCRUB_MONEY = re.compile(r"\$[\d,]+(?:\.\d+)?")
_YEAR = re.compile(r"^(1[89]\d{2}|20\d{2})$")
_LONG_DIGITS = re.compile(r"\d{3,}")


def scrub_query(query: str) -> str:
    """Strip anything that could carry matter content into a CanLII request.

    Removes email addresses, URLs, currency amounts, and digit runs of three or
    more (unit numbers, file numbers, amounts, full dates) while keeping
    four-digit years, which are load-bearing in statute names."""
    q = _SCRUB_EMAIL.sub(" ", query or "")
    q = _SCRUB_URL.sub(" ", q)
    q = _SCRUB_MONEY.sub(" ", q)
    kept: list[str] = []
    for token in q.split():
        bare = token.strip("\"'(),;:.").strip()
        if _LONG_DIGITS.search(bare) and not _YEAR.match(bare):
            continue
        kept.append(token)
    # Collapse the quote damage a dropped token can leave behind.
    out = " ".join(kept)
    if out.count('"') % 2:
        out = out.replace('"', " ")
    return " ".join(out.split()).strip()


def broaden(query: str) -> str:
    """Widen a targeted query by dropping its phrase quoting.

    CanLII OR-es terms, so removing the quotes turns a narrow phrase match into
    a bag of terms and materially widens the result set. Used only when the
    targeted pass came back thin (approved design A: targeted first, broaden
    if empty)."""
    return " ".join(query.replace('"', " ").split())


# --------------------------------------------------------------------------
# Step 1 -- local matter context
# --------------------------------------------------------------------------
def _matter_passages(
    files: list[MatterFile], research_direction: str, embed_model: str
) -> list[str]:
    """Retrieve matter passages relevant to the research direction.

    Reuses the Day-4b scatter-gather so this task sees the matter the same way
    every other cross-document task does. Purely local -- the vector store and the
    embedding model both run on this machine."""
    if not files:
        raise NoMatterContext("This matter has no ingested files.")
    template = get_template(TASK_ID)
    seed = f"{template.retrieval_query} {research_direction}".strip()
    query_vec = embed([seed], model_name=embed_model)[0]
    client = connect()
    scored = search_across_collections(
        client, [f.collection for f in files], query_vec, MATTER_TOP_K
    )
    if not scored:
        raise NoMatterContext(
            "No passages could be retrieved from this matter's documents."
        )
    return [f"[{sc.source} {sc.locator}]\n{sc.text}" for sc in scored]


# --------------------------------------------------------------------------
# Step 2 -- concept + query extraction
# --------------------------------------------------------------------------
def extract_concepts(
    llm: LLMClient,
    passages: list[str],
    research_direction: str,
    jurisdiction: str,
) -> MatterConcepts:
    """One LLM call: matter passages + research direction -> concepts + queries.

    This is the only call that sees raw matter text. Its output is the
    de-identified surface everything downstream works from."""
    raw = llm.complete(
        [
            {"role": "system", "content": build_concept_extraction_prompt(jurisdiction)},
            {
                "role": "user",
                "content": _concept_user_message(passages, research_direction, jurisdiction),
            },
        ]
    )
    data = _first_json_object(raw)
    if data is None:
        raise CaseDiscoveryError(
            "The model did not return a usable set of search concepts. "
            "Try rephrasing the research direction."
        )
    try:
        concepts = MatterConcepts(**data)
    except (ValidationError, TypeError) as e:
        log.warning(f"Concept extraction failed validation: {e}")
        raise CaseDiscoveryError(
            "The model's search concepts could not be read. "
            "Try rephrasing the research direction."
        )

    cleaned: list[GeneratedQuery] = []
    seen: set[str] = set()
    for q in concepts.queries:
        text = scrub_query(q.query)
        if len(text) < 8:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(
            GeneratedQuery(angle=q.angle or "unspecified", query=text,
                           rationale=q.rationale)
        )
    concepts.queries = cleaned[:MAX_QUERIES]
    if len(concepts.queries) < MIN_QUERIES:
        raise CaseDiscoveryError(
            f"Only {len(concepts.queries)} usable CanLII queries could be built "
            f"from this matter and research direction (minimum {MIN_QUERIES}). "
            f"Try a more specific research direction."
        )
    return concepts


def _concept_user_message(
    passages: list[str], research_direction: str, jurisdiction: str
) -> str:
    lines = ["MATTER PASSAGES:", ""]
    lines.extend(f"{p}\n" for p in passages)
    lines.append("RESEARCH DIRECTION:")
    lines.append(research_direction)
    lines.append("")
    lines.append(f"JURISDICTION: {jurisdiction}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Steps 4-5 -- execute, merge, filter, rank
# --------------------------------------------------------------------------
def _merge(pool: dict[str, CanLIICase], hits: list[CanLIICase], angle: str) -> int:
    """Fold one query's results into the pool, deduping by citation.

    Records which angle found each case and the best position it reached. A
    case surfaced by several independent angles is stronger evidence of
    relevance than one that appears once, and that is a ranking signal."""
    for hit in hits:
        key = hit.dedupe_key
        existing = pool.get(key)
        if existing is None:
            hit.found_by = [angle]
            pool[key] = hit
        else:
            if angle not in existing.found_by:
                existing.found_by.append(angle)
            existing.best_rank = min(existing.best_rank, hit.best_rank)
    return len(hits)


def _jurisdiction_score(case: CanLIICase, jurisdiction: str) -> float:
    """Whether this court's decisions carry force in the requested jurisdiction.

    This scores AUTHORITY, not geography. The Supreme Court of Canada is not a
    foreign court to an Ontario matter -- it is the highest authority in it -- so
    tier 1 scores a full match in every jurisdiction. Scoring the SCC as a
    "federal, therefore partial" match is what pushed binding authority below
    the Superior Court in the first cut of this function.

    Note that an Ontario-scoped run DEMOTES out-of-province authority rather
    than dropping it (approved). SoW 4.3.1 requires out-of-jurisdiction
    authority to surface when no Ontario authority is on point; a hard filter
    would make that rule impossible to satisfy."""
    if case.court.tier == 1:
        return 1.0  # binding everywhere in Canada
    juris = case.court.jurisdiction
    if jurisdiction == "Ontario":
        if juris == "on":
            return 1.0
        if juris == "ca":
            # Federal Court / FCA / Tax Court: a real court, but a different
            # subject-matter jurisdiction. A genuine partial match.
            return 0.35
        return 0.15
    if jurisdiction == "Federal":
        return 1.0 if juris == "ca" else 0.15
    return 1.0 if juris in ("on", "ca") else 0.6


def _passes_filters(
    case: CanLIICase, jurisdiction: str, earliest_year: int
) -> bool:
    """Hard filters -- correctness rules, applied before any scoring."""
    if case.database_id in canlii.EXCLUDED_DATABASES:
        return False
    year = case.effective_year
    if year is not None and year < earliest_year:
        return False
    if jurisdiction == "Federal" and case.court.jurisdiction != "ca":
        return False
    return True


def _recency_score(case: CanLIICase, date_range_years: int) -> float:
    """Recency, floored at 0.15.

    Leading authority is routinely old -- the top-ranked Ontario condominium
    common-elements case on CanLII dates from 1975 -- so a hard age decay would
    bury exactly the cases a lawyer most wants. Recency is a tiebreaker at
    W_RECENCY=0.10, not a filter."""
    year = case.effective_year
    if not year:
        return 0.15
    age = max(0, dt.date.today().year - year)
    fresh = max(0.0, 1.0 - age / max(1, date_range_years))
    return 0.15 + 0.85 * fresh


def score_stage1(
    case: CanLIICase, jurisdiction: str, date_range_years: int
) -> tuple[float, dict[str, float]]:
    """Rank on signals available without an extra API call.

    Everything here comes from the search result itself: the databaseId (court),
    the citation (year), and this run's own bookkeeping (which angles found it,
    how highly CanLII ranked it)."""
    court = case.court.weight
    juris = _jurisdiction_score(case, jurisdiction)
    angles = min(1.0, max(0, len(case.found_by) - 1) / 3.0)
    rank = max(0.0, 1.0 - case.best_rank / float(SEARCH_PAGE))
    recency = _recency_score(case, date_range_years)
    breakdown = {
        "court": W_COURT * court,
        "jurisdiction": W_JURISDICTION * juris,
        "angles": W_ANGLES * angles,
        "search_rank": W_SEARCH_RANK * rank,
        "recency": W_RECENCY * recency,
    }
    return sum(breakdown.values()), breakdown


# Words that carry no discriminating power in a legal catchword string. Kept
# small and legal-specific; a general stopword list would strip terms like
# "against" that do distinguish one catchword line from another.
_SUBJECT_STOPWORDS = frozenset("""
a an the and or of to in on for by with at from as is are was were be been
that this these those it its any all such which who whom whose not no
case cases court courts law laws legal act section sections s ss
appeal appellant respondent applicant application motion order
""".split())

_TOKEN = re.compile(r"[a-z][a-z\-']{2,}")


def _tokens(text: str) -> set[str]:
    return {
        t for t in _TOKEN.findall((text or "").lower())
        if t not in _SUBJECT_STOPWORDS
    }


# How hard a published-but-unrelated catchword string counts against a case.
# See subject_signal.
SUBJECT_MISMATCH_PENALTY = 0.75


def subject_signal(case: CanLIICase, concept_terms: list[str]) -> float:
    """Subject-matter evidence from CanLII's catchwords, in [-PENALTY, 1].

    CanLII's `keywords` field is a curated catchword string ("Property -
    Condominium law - Exclusive use common elements - Maintenance and repair
    obligations - ..."), which makes it the best subject signal available. It
    only exists after enrichment, which is why this is stage 2.

    Crucially this returns a SIGNED value, because "no catchwords published"
    and "catchwords published that overlap nothing" are different facts and
    were conflated in the first cut of this function:

      * No catchwords at all -> 0.0. We have no evidence either way, and a case
        should not be punished for CanLII's editorial coverage.
      * Catchwords present, zero overlap -> NEGATIVE. This is real evidence of
        irrelevance, not an absence of evidence. Treating it as neutral is what
        let a Supreme Court decision on Aboriginal fiduciary duties and two
        automotive class actions onto an 18-case condominium-repair shortlist:
        with the subject term zeroed, court tier and recency alone were enough
        to carry them.
      * Otherwise -> positive, scaled by overlap.

    The concept side of the comparison is deliberately broad (legal issues,
    statutes, party types, fact pattern, and the research direction), so zero
    overlap against all of it is a strong signal rather than a vocabulary
    accident."""
    haystack = _tokens(f"{case.keywords or ''} {case.topics or ''}")
    needles = _tokens(" ".join(concept_terms))
    if not haystack or not needles:
        return 0.0
    shared = len(haystack & needles)
    if shared == 0:
        return -SUBJECT_MISMATCH_PENALTY
    return min(1.0, shared / float(min(len(needles), 25)))


# --------------------------------------------------------------------------
# Step 7 -- preliminary notes
# --------------------------------------------------------------------------
def fallback_note(case: CanLIICase) -> str:
    """A note assembled from metadata alone, with no model involvement.

    Used when the model omits a case or returns an unmatched citation. It is
    deliberately duller than the generated notes: a missing card, or a card
    silently carrying another case's note, would both be worse than a plain
    restatement of what CanLII says about this case."""
    if case.keywords:
        first = case.keywords.split("|")[0].strip()
        if len(first) > 220:
            first = first[:220].rstrip() + "..."
        return (
            f"CanLII classifies this decision under: {first}. "
            f"No further assessment is possible without reading the case."
        )
    if case.topics:
        return (
            f"CanLII files this decision under the topic \"{case.topics}\". "
            f"No further assessment is possible without reading the case."
        )
    found = ", ".join(case.found_by) or "the search"
    return (
        f"Surfaced by the \"{found}\" search; CanLII publishes no catchwords for "
        f"this decision. No assessment is possible without reading the case."
    )


def generate_notes(
    llm: LLMClient,
    concepts: MatterConcepts,
    research_direction: str,
    cases: list[CanLIICase],
) -> dict[str, str]:
    """One LLM call for the whole shortlist -> {dedupe_key: note}.

    The prompt receives the de-identified CONCEPTS and the case metadata --
    never the matter passages. Sending privileged document text to describe a
    case whose text we do not have would put confidential content into a prompt
    that cannot possibly need it.

    Returns only notes that matched a case in `cases`; the caller fills the
    rest from `fallback_note`."""
    if not cases:
        return {}
    raw = llm.complete(
        [
            {"role": "system", "content": build_case_note_prompt()},
            {
                "role": "user",
                "content": _note_user_message(concepts, research_direction, cases),
            },
        ]
    )
    data = _first_json_object(raw)
    if not data or "notes" not in data:
        log.warning("Case-note generation returned no usable JSON; "
                    "every note falls back to metadata.")
        return {}

    by_key = {c.dedupe_key: c for c in cases}
    out: dict[str, str] = {}
    for entry in data.get("notes") or []:
        try:
            note = CaseNote(**entry)
        except (ValidationError, TypeError):
            continue
        key = canlii._CITATION_NOISE.sub("", note.citation or "").strip().casefold()
        text = " ".join((note.note or "").split())
        if key in by_key and text:
            out[key] = text
    return out


def _note_user_message(
    concepts: MatterConcepts, research_direction: str, cases: list[CanLIICase]
) -> str:
    lines = ["MATTER CONCEPTS (no document text is provided, by design):", ""]
    lines.extend(concepts.summary_lines())
    lines.append("")
    lines.append("RESEARCH DIRECTION:")
    lines.append(research_direction)
    lines.append("")
    lines.append("CANDIDATE CASES (metadata only -- no case text exists for these):")
    lines.append("")
    for i, c in enumerate(cases, 1):
        lines.append(f"{i}. Citation: {c.citation}")
        lines.append(f"   Title: {c.title}")
        lines.append(f"   Court: {c.court.name} ({c.court.binding_label})")
        lines.append(f"   Decided: {c.display_date}")
        lines.append(f"   CanLII topics: {c.topics or '(none published)'}")
        lines.append(f"   CanLII catchwords: {c.keywords or '(none published)'}")
        lines.append(f"   Surfaced by search angle(s): {', '.join(c.found_by) or 'n/a'}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# JSON helper
# --------------------------------------------------------------------------
_JSON_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)


def _first_json_object(text: str) -> dict | None:
    """Pull a JSON object out of a completion, fenced, bare, or truncated.

    The truncation path is not defensive padding -- it is the failure mode that
    actually occurs. Both of this task's prompts ask for a long structured
    block, and a completion cut at the token limit ends mid-array with no
    closing fence. The queries or notes generated before the cut are perfectly
    good; discarding them because the model ran out of room would turn a
    complete-enough result into an error page."""
    if not text:
        return None
    for match in _JSON_FENCE.finditer(text):
        data = _load(match.group(1))
        if data is not None:
            return data
    # Unclosed fence: a truncated completion never emits the closing ```.
    fence = re.search(r"```(?:json)?\s*\n", text, re.IGNORECASE)
    if fence:
        data = _load(text[fence.end():])
        if data is not None:
            return data
    start = text.find("{")
    if start != -1:
        return _load(text[start:])
    return None


def _load(fragment: str) -> dict | None:
    """json.loads, then a bracket-closing repair if the fragment was cut off."""
    fragment = fragment.strip()
    if not fragment:
        return None
    try:
        data = json.loads(fragment)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    repaired = _close_open_json(fragment)
    if repaired is None:
        return None
    try:
        data = json.loads(repaired)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _close_open_json(fragment: str) -> str | None:
    """Close the brackets a truncated JSON object left open.

    Walks the fragment tracking string state and nesting, discards any trailing
    partial value, and appends the closers. Returns None when there is nothing
    recoverable."""
    start = fragment.find("{")
    if start == -1:
        return None
    stack: list[str] = []
    in_string = False
    escaped = False
    last_safe = -1  # index just after the last structurally complete element
    for i in range(start, len(fragment)):
        ch = fragment[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
            last_safe = i + 1
        elif ch == ",":
            last_safe = i  # cut BEFORE the comma; a trailing comma is invalid
    if last_safe <= start:
        return None
    # Re-walk the kept prefix so the closers match what actually remains open.
    kept = fragment[start:last_safe]
    stack = []
    in_string = False
    escaped = False
    for ch in kept:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    if in_string:
        return None
    return kept + "".join(reversed(stack))


# --------------------------------------------------------------------------
# Fallback browse targets
# --------------------------------------------------------------------------
# Last resort when no keyword query matched anything at all. Browsing a court's
# recent docket has NO relevance signal, so it is labelled as a fallback in the
# UI and never runs when search produced results.
_BROWSE_TARGETS = {
    "Ontario": ["onca", "onsc", "onscdc"],
    "Federal": ["fca", "fct"],
    "All Canada": ["csc-scc", "onca", "onsc"],
}


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def run_case_discovery(
    files: list[MatterFile],
    matter_id: int,
    matter_name: str,
    structured_inputs: dict,
) -> CaseDiscoveryResult:
    """Matter documents + a research direction -> a ranked CanLII shortlist."""
    research_direction = (structured_inputs.get("research_direction") or "").strip()
    if not research_direction:
        raise CaseDiscoveryError("A research direction is required.")

    jurisdiction = structured_inputs.get("jurisdiction") or DEFAULT_JURISDICTION
    if jurisdiction not in JURISDICTION_CHOICES:
        jurisdiction = DEFAULT_JURISDICTION
    max_cases = _clamp_int(
        structured_inputs.get("max_cases"), DEFAULT_MAX_CASES, 1, MAX_CASES_LIMIT
    )
    date_range_years = _clamp_int(
        structured_inputs.get("date_range_years"), DEFAULT_DATE_RANGE, 1,
        DATE_RANGE_LIMIT,
    )
    earliest_year = dt.date.today().year - date_range_years

    embed_model = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
    model = os.environ.get("MODEL", "xiaomi/mimo-v2.5-pro")

    warnings: list[str] = []
    funnel = DiscoveryFunnel()

    # -- 1/2/3: local retrieval -> concepts -> scrubbed queries ------------
    passages = _matter_passages(files, research_direction, embed_model)
    llm = LLMClient(model=model)
    log.info("Extracting legal concepts and building CanLII queries ...")
    concepts = extract_concepts(llm, passages, research_direction, jurisdiction)
    log.info(f"Built {len(concepts.queries)} CanLII queries.")

    client = CanLIIClient()
    budget_warning = client.budget.warning()

    # -- 4: targeted pass --------------------------------------------------
    pool: dict[str, CanLIICase] = {}
    executed: list[GeneratedQuery] = []
    throttled_after: int | None = None

    for gq in concepts.queries:
        try:
            hits = client.search(gq.query, result_count=SEARCH_PAGE)
        except canlii.CanLIIThrottled:
            throttled_after = len(executed)
            break
        except canlii.CanLIIBudgetExceeded:
            raise
        except canlii.CanLIIError as e:
            warnings.append(f"Query \"{gq.query}\" failed: {e}")
            continue
        executed.append(gq)
        funnel.raw_hits += _merge(pool, hits, gq.angle)

    if throttled_after is not None:
        warnings.append(
            f"CanLII rate-limited this run after {throttled_after} of "
            f"{len(concepts.queries)} queries. This shortlist is INCOMPLETE - "
            f"re-run it in a few minutes for the full set."
        )

    # -- 4b: broaden if the targeted pass came back thin -------------------
    survivors = [
        c for c in pool.values()
        if _passes_filters(c, jurisdiction, earliest_year)
    ]
    if len(survivors) < BROADEN_THRESHOLD and throttled_after is None:
        log.info(
            f"Targeted pass yielded {len(survivors)} case(s); broadening."
        )
        funnel.broadened = True
        for gq in concepts.queries[:MAX_BROADENED_QUERIES]:
            wide = broaden(gq.query)
            if wide.casefold() == gq.query.casefold():
                continue
            try:
                hits = client.search(wide, result_count=SEARCH_PAGE)
            except canlii.CanLIIThrottled:
                break
            except canlii.CanLIIError:
                continue
            executed.append(
                GeneratedQuery(
                    angle=f"{gq.angle} (broadened)", query=wide,
                    rationale="Broadened from the targeted query above because "
                              "the targeted pass returned too few cases.",
                )
            )
            funnel.raw_hits += _merge(pool, hits, f"{gq.angle} (broadened)")
        survivors = [
            c for c in pool.values()
            if _passes_filters(c, jurisdiction, earliest_year)
        ]

    # -- 4c: browse fallback, only when search found nothing at all --------
    if not survivors and throttled_after is None:
        log.info("No cases from any search; falling back to court browse.")
        funnel.used_browse_fallback = True
        warnings.append(
            "No CanLII keyword search matched this research direction, so the "
            "tool fell back to listing recent decisions from the relevant "
            "courts. These are NOT relevance-matched - they are simply recent."
        )
        for db in _BROWSE_TARGETS.get(jurisdiction, _BROWSE_TARGETS["All Canada"]):
            try:
                hits = client.browse_database(db, result_count=SEARCH_PAGE)
            except canlii.CanLIIError:
                continue
            executed.append(
                GeneratedQuery(
                    angle="browse fallback", query=f"(recent decisions of {db})",
                    rationale="No keyword search matched; listing this court's "
                              "recent docket instead.",
                )
            )
            funnel.raw_hits += _merge(pool, hits, "browse fallback")
        survivors = [
            c for c in pool.values()
            if _passes_filters(c, jurisdiction, earliest_year)
        ]

    funnel.queries_executed = len(executed)
    funnel.unique_cases = len(pool)
    funnel.after_filters = len(survivors)

    if not survivors:
        raise CaseDiscoveryError(
            "No CanLII cases matched this research direction within the "
            f"{date_range_years}-year window. Try a broader research "
            f"direction, a wider date range, or a different jurisdiction."
        )

    # -- 5: stage-1 ranking ------------------------------------------------
    stage1: list[tuple[float, dict[str, float], CanLIICase]] = []
    for case in survivors:
        score, breakdown = score_stage1(case, jurisdiction, date_range_years)
        stage1.append((score, breakdown, case))
    stage1.sort(key=lambda t: (-t[0], t[2].court.tier, t[2].citation))

    # -- 6: enrich the top slice, then re-rank ----------------------------
    enrich_n = min(max_cases + ENRICH_HEADROOM, ENRICH_CAP, len(stage1))
    enriched: list[tuple[float, dict[str, float], CanLIICase]] = []
    concept_terms = concepts.concept_terms() + [research_direction]
    enrich_throttled = False
    for score, breakdown, case in stage1[:enrich_n]:
        if not enrich_throttled:
            try:
                client.case_metadata(case)
                funnel.enriched += 1
            except canlii.CanLIIThrottled:
                enrich_throttled = True
                warnings.append(
                    "CanLII rate-limited the metadata lookup partway through, "
                    "so some cases below show no catchwords or decision date."
                )
            except canlii.CanLIIBudgetExceeded:
                enrich_throttled = True
                warnings.append(
                    "The daily CanLII call budget was reached partway through, "
                    "so some cases below show no catchwords or decision date."
                )
        # Enrichment supplies the real decision date; re-apply the date filter
        # in case the citation year disagreed with it.
        if not _passes_filters(case, jurisdiction, earliest_year):
            continue
        subject = subject_signal(case, concept_terms)
        breakdown = dict(breakdown, subject=W_SUBJECT * subject)
        enriched.append((sum(breakdown.values()), breakdown, case))

    enriched.sort(
        key=lambda t: (
            -t[0],
            t[2].court.tier,
            -(t[2].effective_year or 0),
            t[2].citation,
        )
    )
    shortlist = enriched[:max_cases]

    # -- 7: preliminary notes ---------------------------------------------
    log.info(f"Generating preliminary notes for {len(shortlist)} case(s) ...")
    notes = generate_notes(
        llm, concepts, research_direction, [c for _s, _b, c in shortlist]
    )
    ranked: list[RankedCase] = []
    for score, breakdown, case in shortlist:
        note = notes.get(case.dedupe_key)
        ranked.append(
            RankedCase(
                case=case,
                score=score,
                breakdown=breakdown,
                note=note or fallback_note(case),
                note_is_fallback=note is None,
            )
        )
    funnel.shown = len(ranked)

    if any(r.note_is_fallback for r in ranked):
        n = sum(1 for r in ranked if r.note_is_fallback)
        warnings.append(
            f"{n} case(s) below show a note assembled directly from CanLII's "
            f"catchwords because the model did not return one for them."
        )

    # -- audit -------------------------------------------------------------
    # Records WHAT WAS ASKED OF CANLII and WHAT CAME BACK. The queries are the
    # scrubbed, concept-only strings that actually left this machine, so the log
    # doubles as the reviewable record that no matter content was transmitted.
    # No case content is logged because none exists.
    audit.log_event(
        "canlii_case_discovery",
        matter_id=matter_id,
        task=TASK_ID,
        research_direction=research_direction,
        jurisdiction=jurisdiction,
        date_range_years=date_range_years,
        max_cases=max_cases,
        queries_executed=[
            {"angle": q.angle, "query": q.query} for q in executed
        ],
        cases_returned=[r.case.citation for r in ranked],
        canlii_calls=client.calls_made,
        broadened=funnel.broadened,
        used_browse_fallback=funnel.used_browse_fallback,
        unique_cases=funnel.unique_cases,
        after_filters=funnel.after_filters,
        warnings=warnings,
    )

    return CaseDiscoveryResult(
        matter_id=matter_id,
        matter_name=matter_name,
        research_direction=research_direction,
        jurisdiction=jurisdiction,
        max_cases=max_cases,
        date_range_years=date_range_years,
        concepts=concepts,
        queries=executed,
        cases=ranked,
        funnel=funnel,
        warnings=warnings,
        model=model,
        timestamp=dt.datetime.now(dt.timezone.utc).isoformat(),
        canlii_calls=client.calls_made,
        budget_warning=budget_warning,
    )


def _clamp_int(value, default: int, low: int, high: int) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default
    return max(low, min(high, n))
