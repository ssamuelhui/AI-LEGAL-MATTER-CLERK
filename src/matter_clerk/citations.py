"""Canadian case-citation extraction (Phase 2b).

Pulls case citations out of model-generated legal analysis so they can be
verified against CanLII before the lawyer sees them. Pure text processing: no
network, no LLM, no state. `verification.py` does the checking.

WHY THIS IS A SEPARATE TYPE FROM `citation.Citation`
----------------------------------------------------
`citation.Citation` is a pointer into a MATTER DOCUMENT — a filename plus a page
locator plus the snippet that grounds a fact. `CaseCitation` is a reference to
an EXTERNAL legal authority. They are verified by different means against
different sources and carry different risk: a wrong matter citation points at
the wrong page of a document the lawyer has, whereas a wrong case citation may
point at a case that does not exist. Two classes named `Citation` in one
pipeline would be a permanent source of bugs, so this one is `CaseCitation`.

OVER-EXTRACTION IS NOT COSMETIC
-------------------------------
Verification runs in strict mode: a citation that does not resolve is STRIPPED
from the lawyer's document. So a false extraction ("2020 Revenue 15" read as a
citation) does not merely waste an API call — it deletes text from a legal
memorandum. That is why the court token is checked against a whitelist derived
from CanLII's own database identifiers rather than accepted as any capitalised
word.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Court tokens
# --------------------------------------------------------------------------
# Generated from the 409 databaseId values returned by GET /caseBrowse/en/
# (2026-08-27), uppercased and restricted to the alphabetic ones — a neutral
# citation's court token is the database id in capitals for all but a handful of
# courts, which are listed in _CITATION_ALIASES below.
_CATALOG_TOKENS = """
ABAER ABCA ABCDRT ABCGYARB ABCI ABCJ ABCPA ABCPSDC ABCPT ABCTIAB ABEAB ABECARB
ABELARB ABESDAB ABESU ABGAA ABHRAAT ABHRC ABKB ABLCB ABLCSAB ABLERB ABLPRT
ABLRB ABLS ABMGB ABOHSAB ABOIPC ABPLAB ABRECA ABRTDRS ABSDAB ABSEC ABSRA ABSRB
ABTSB ABWCAC BCCA BCCCALAB BCCDS BCCNM BCCOHP BCCPS BCCRT BCCTC BCEAB BCEG
BCEST BCFAC BCFST BCHAB BCHPRB BCHRT BCIPC BCITAB BCLA BCLCRB BCLRB BCOGAT
BCORL BCPAAB BCPC BCRB BCREC BCRMB BCSC BCSEC BCSFI BCSP BCSRB BCSRE BCWCAT
CACI CACP CACT CAIIROC CAMFDA CAPSDPT CBC CHRT CIRB CISR CM EPTC FCA FCT FOREP
FPCA ICCRC LSBC MBCA MBCPSDC MBHAB MBHRC MBKB MBLA MBLB MBLS MBPC MBSEC NBAPAB
NBBIHRA NBCA NBCPH NBEUB NBFC NBKB NBLA NBLEB NBLS NBOMB NBPC NBREA NBSEC
NBWHSCC NLCA NLCPS NLHRC NLIPC NLLA NLLRB NLLS NLPB NLPC NLSCTD NSAWAB NSBS
NSCA NSCPS NSFOIPOP NSHRC NSLA NSLB NSLRB NSLST NSOHSAP NSPC NSPR NSPRB NSSC
NSSEC NSSIRT NSSM NSUARB NSWCAT NTAAT NTCA NTHRAP NTIPC NTLA NTLLB NTLS NTLSB
NTRO NTSC NTSEC NTTC NTWCAT NTYDAB NTYJC NUCA NUCJ NUHRT NUIPC NULA NULS NUSEC
NUWCAT NUYC OHSTC OIC ONACRB ONAFRAAT ONAGC ONAPE ONARB ONBAH ONBCC ONCA
ONCASPD ONCAT ONCCB ONCDHO ONCECE ONCFSRB ONCICB ONCJ ONCMLTO ONCMT ONCMTO
ONCNO ONCO ONCOCOO ONCONRB ONCOTO ONCPA ONCPC ONCPD ONCPDC ONCPO ONCPS ONCRB
ONCRPO ONCSWSSW ONCTCMPAO ONCVO ONDR ONERT ONFSC ONFSCDRS ONFST ONGSB ONHPARB
ONHRAP ONHRT ONHSARB ONIOP ONIPC ONLA ONLAT ONLRB ONLST ONLT ONLTB ONMB ONMIC
ONMLC ONMRITDT ONNFPPB ONOCA ONOCT ONOEB ONOMBUD ONPEHT ONPPRB ONPSGB ONRB ONRC
ONRCDSO ONRIBODC ONSBT ONSC ONSCDC ONSEC ONSET ONST ONTLAB ONWSIAT ONWSIB
PEIHRC PEIPC PEIRAC PEIWCAT PELA PELRB PEPC PESCAD PESCTD PSLREB PSSRB PSST
QCADMAQ QCAGQ QCAMF QCAMP QCBDRVM QCCA QCCAI QCCALP QCCDBQ QCCDCCOQ QCCDCHA
QCCDCHAD QCCDCM QCCDCRIM QCCDCSF QCCDHJ QCCDNQ QCCDOII QCCDOIIA QCCDOIQ QCCDOMV
QCCDOOOQ QCCDOPQ QCCDOSFQ QCCDOTTDQ QCCDP QCCDPPQ QCCDRHRI QCCES QCCFP QCCJA
QCCLP QCCM QCCMEQ QCCMMTQ QCCMNQ QCCMQ QCCNESST QCCPA QCCPTAQ QCCQ QCCQLC
QCCRAAAP QCCRT QCCS QCCSE QCCSJ QCCSST QCCT QCCTQ QCCVM QCDAG QCOACIQ QCOAGBRN
QCOAGQ QCOAPQ QCOAQ QCOARQ QCOCHQ QCOCQ QCODLQ QCODQ QCOEAQ QCOEQ QCOHDQ QCOIFQ
QCOLF QCOOAQ QCOOQ QCOPDQ QCOPGQ QCOPIQ QCOPODQ QCOPPQ QCOPQ QCOPSQ QCOTIMRO
QCOTMQ QCOTPQ QCOTSTCFQ QCOTTIAQ QCOUQ QCRACJ QCRBQ QCRDE QCRDL QCRMAAQ QCTAA
QCTACARRA QCTAQ QCTAT QCTDP QCTP QCTT SKAC SKAIA SKATMPA SKCA SKCP SKFCA SKHRC
SKHRT SKIPC SKKB SKLA SKLGB SKLRB SKLSS SKMBR SKMT SKORT SKPC SKPMB SKREC SKSEC
SKSMB SKWCBAT SOPF TATC TMOB UKPC VRAB YKCA YKHRC YKSC YKSM YKTC YTLA YTOIPC
YTPSLRB YTRTO YTTLRB
"""

# Neutral-citation tokens whose CanLII databaseId differs, or which name a court
# the catalog omits entirely (the pre-accession Queen's Bench courts and the
# Ontario High Court — see the Phase-2a note on catalog incompleteness).
#
# Values are the databaseId used to build the lookup path. CanLII IGNORES that
# path segment (verified: caseBrowse/en/zzzz/2020onca471/ returns the ONCA
# case), so a wrong value here cannot cause a real citation to fail — it only
# keeps the request legible.
_CITATION_ALIASES: dict[str, str] = {
    "SCC": "csc-scc",       # Supreme Court of Canada — not "csc-scc" uppercased
    "CSC": "csc-scc",       # French-language form
    "FC": "fct",            # Federal Court cites as "2020 FC 123"
    "CF": "fct",
    "TCC": "cci-tcc",       # Tax Court of Canada cites as "2020 TCC 45"
    "CCI": "cci-tcc",
    "CMAC": "cmac-cacm",
    "CACM": "cmac-cacm",
    "NWTSC": "ntsc",        # NWT cites as NWTSC, database is ntsc
    "NWTCA": "ntca",
    "NWTTC": "nttc",
    # Historic courts: real, still cited, absent from the live catalog.
    "ABQB": "abqb", "SKQB": "skqb", "MBQB": "mbqb", "NBQB": "nbqb",
    "ONHCJ": "onhcj",
}

COURT_TOKENS: frozenset[str] = frozenset(
    _CATALOG_TOKENS.split()
) | frozenset(_CITATION_ALIASES)


def database_for(court_token: str) -> str:
    """CanLII databaseId for a neutral-citation court token.

    Cosmetic only — CanLII resolves a case on its caseId alone and ignores the
    database path segment."""
    token = (court_token or "").upper()
    return _CITATION_ALIASES.get(token, token.lower())


# --------------------------------------------------------------------------
# Markers written into the answer text
# --------------------------------------------------------------------------
# ASCII-and-WinAnsi only, deliberately. These markers travel in `answer`, which
# is the single source rendered by the web page, the Word export AND the PDF
# export. ReportLab's Helvetica is WinAnsi-encoded and has no U+2713 check mark,
# so a tick baked in here would render as a black box (or vanish) in the PDF a
# lawyer forwards onward. The em-dash IS in WinAnsi and matches the existing
# [ELEMENTS REQUIRED — ...] house style.
#
# The web template decorates MARK_VERIFIED into a green check-mark badge at
# render time, which is where a tick is safe.
MARK_VERIFIED = "[verified in CanLII]"


def mark_removed(citation: str) -> str:
    return f"[REMOVED — citation not verified: {citation}]"


def mark_mismatch(citation: str, claimed: str, actual: str) -> str:
    return (
        f"[CITATION MISMATCH — {citation} is “{actual}”, "
        f"not “{claimed}” — verify before relying on this]"
    )


def mark_unverifiable(citation: str) -> str:
    return f"[UNVERIFIED — CanLII could not be reached to check: {citation}]"


def mark_unsupported(citation: str) -> str:
    return (
        f"[UNVERIFIED — citation format cannot be checked against CanLII: "
        f"{citation}]"
    )


# Recognises every marker this module writes, so exports can highlight them the
# way they already highlight the pleading gap markers.
#
# Tolerates ONE level of nested brackets. This is not hypothetical tidiness: a
# reporter-only citation is itself bracketed, so the unsupported-format marker
# reads
#     [UNVERIFIED — ... cannot be checked ...: [2005] 2 S.C.R. 601]
# and a pattern ending at the first "]" clips it mid-citation, leaving
# "2 S.C.R. 601]" outside the marker and unstyled.
VERIFICATION_MARKER_PATTERN = (
    r"\[(?:REMOVED|CITATION MISMATCH|UNVERIFIED|verified in CanLII)"
    r"(?:[^\[\]]|\[[^\[\]]*\])*\]"
)
VERIFICATION_MARKER = re.compile(VERIFICATION_MARKER_PATTERN, re.DOTALL)


# --------------------------------------------------------------------------
# The citation model
# --------------------------------------------------------------------------
@dataclass
class CaseCitation:
    """One case citation found in model output.

    `span` covers the text that will be replaced or removed. It deliberately
    extends past the neutral citation to swallow any parallel reporter citation
    that follows, so stripping "2020 ONCA 471, 149 O.R. (3d) 481" does not leave
    a dangling ", 149 O.R. (3d) 481" pointing at nothing.
    """

    full_citation: str          # normalised: "2020 ONCA 471"
    raw_text: str               # exactly as it appeared, including any tail
    court_token: str            # "ONCA"; "" for a reporter-only citation
    year: int
    decision_number: int        # 0 for a reporter-only citation
    span: tuple[int, int]
    context_sentence: str
    case_name: str | None = None
    # A reporter-only citation ([2005] 2 S.C.R. 601) has no derivable CanLII
    # caseId and cannot be checked by any means the API offers.
    supported: bool = True

    @property
    def case_id(self) -> str:
        """The CanLII caseId. MUST be lowercase — CanLII rejects
        `2020ONCA471` with 'Data id ... is invalid'."""
        if not self.supported:
            return ""
        return f"{self.year}{self.court_token.lower()}{self.decision_number}"

    @property
    def database_id(self) -> str:
        return database_for(self.court_token)


# --------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------
_YEAR = r"(1[89]\d{2}|20\d{2})"

# "2020 ONCA 471", and the no-space form "2020ONCA471" a sloppy model emits.
# \s* rather than \s+ handles both; the court token is validated against
# COURT_TOKENS afterwards, which is what keeps this from matching prose.
_NEUTRAL = re.compile(rf"\b{_YEAR}\s*([A-Za-z]{{2,10}})\s*(\d{{1,6}})\b")

# "1975 CanLII 499 (ON CA)" — CanLII's own citation series for cases decided
# before neutral citations existed. The jurisdiction suffix is informational;
# the caseId is built from the CanLII number.
_CANLII = re.compile(
    rf"\b{_YEAR}\s*CanLII\s*(\d{{1,6}})\s*(\([^)]{{2,24}}\))?", re.IGNORECASE
)

# Printed reporters, used both to swallow parallel-citation tails and to detect
# reporter-only citations. A whitelist rather than a general pattern: a loose
# "digits + capitalised abbreviation + digits" rule swallows ordinary prose.
_REPORTERS = (
    r"(?:S\.?C\.?R\.?|O\.?R\.?|D\.?L\.?R\.?|C\.?C\.?C\.?|R\.?F\.?L\.?"
    r"|W\.?W\.?R\.?|C\.?P\.?R\.?|O\.?A\.?C\.?|N\.?R\.?|C\.?B\.?R\.?"
    r"|E\.?T\.?R\.?|R\.?P\.?R\.?|C\.?L\.?R\.?|A\.?C\.?W\.?S\.?"
    r"|C\.?E\.?L\.?R\.?|M\.?P\.?L\.?R\.?|B\.?L\.?R\.?|C\.?R\.?)"
)

# ", 149 O.R. (3d) 481" / ", [2021] 1 SCR 32" — one or more, following a
# neutral citation. Note the series marker is "(3d)", not "(3rd)": law reports
# use that form, and a pattern accepting only st/nd/rd/th silently fails to
# swallow the tail on the most common reporter citation in Ontario practice.
_SERIES = r"(?:\s*\(\d+\s*(?:st|nd|rd|th|d)\))?"
_PARALLEL_TAIL = re.compile(
    rf"(?:\s*,\s*(?:\[{_YEAR}\]\s*)?\d{{1,4}}\s*{_REPORTERS}"
    rf"{_SERIES}\s*\d{{1,4}})+"
)

# "[2005] 2 S.C.R. 601" standing alone — real, common, and unverifiable.
_REPORTER_ONLY = re.compile(
    rf"\[{_YEAR}\]\s*(\d{{1,3}})\s*{_REPORTERS}{_SERIES}\s*(\d{{1,4}})"
)

# Matter-document citations — [Filename.pdf p.4] — are emphatically NOT case
# citations and must never be extracted, verified, or stripped.
_MATTER_CITATION = re.compile(r"\[[^\]]*?\b(?:p\.\s*\d+|from\s+[^\]]+,\s*\d)[^\]]*?\]")


# --------------------------------------------------------------------------
# Case-name detection
# --------------------------------------------------------------------------
# Words that end a case name when scanning backwards — a citation is nearly
# always preceded either by the style of cause or by ordinary prose, and these
# mark the boundary.
# Abbreviations whose trailing period does NOT end a sentence. Without these,
# every case name containing "No.", "v.", "Inc." or a judge's initial is cut in
# half before it can be matched: "... Corporation No. 590 v. Registered Owners"
# would be truncated at "No. ".
#
# Checked procedurally rather than with a look-behind — Python requires
# fixed-width look-behinds, and these are not.
_ABBREVIATIONS = frozenset("""
no nos v vs inc ltd ltee corp co cie bros assn dept div ont can que alta sask
st ste mr mrs ms dr hon j jj cj ja jja para paras s ss c cc art r re ex al et
""".split())

# A colon is deliberately NOT a break. "A fabricated case: Smith v. Jones,
# 2027 ONCA 999" is one sentence, and cutting at the colon would put a truncated
# fragment into the audit log as the "extraction context" for a stripped
# citation — exactly the field a lawyer reads to understand what was removed.
#
# A bare newline is NOT a break either, because model output is soft-wrapped and
# a style of cause routinely straddles a line end:
#
#     ... authority is Metropolitan Toronto Condominium Corporation No. 590 v.
#     Registered Owners, 2020 ONCA 471 ...
#
# Treating that newline as a sentence boundary makes the case name invisible to
# the lookback, which silently switches OFF name-mismatch checking for most real
# citations — the most valuable check this module performs. Only a structural
# break counts: a blank line, or a line starting a new heading, bullet, or
# numbered paragraph.
_PUNCT_BREAK = re.compile(
    r"([.;!?])\s"
    r"|\n(?=\s*(?:\n|#{1,6}\s|[-*+]\s|\d+[.)]\s|\*\*))"
)


def _last_sentence_start(window: str) -> int:
    """Index just after the last real sentence break in `window`.

    A period counts as a break unless the word before it is a known legal
    abbreviation or a single capital letter (a judge's or party's initial)."""
    start = 0
    for m in _PUNCT_BREAK.finditer(window):
        if m.group(1) == ".":
            preceding = re.search(r"([A-Za-z]+)\.$", window[: m.start() + 1])
            if preceding:
                word = preceding.group(1)
                if word.lower() in _ABBREVIATIONS or (
                    len(word) == 1 and word.isupper()
                ):
                    continue
        start = m.end()
    return start

_NAME_CORE = re.compile(
    r"([A-Z][\w'’.\-]*(?:\s+[\w'’.\-&()]+){0,7}?)"
    r"\s+v[.s]?\s+"
    r"([A-Z][\w'’.\-]*(?:\s+[\w'’.\-&()]+){0,7}?)"
    r"\s*,?\s*$"
)
_RE_NAME = re.compile(r"((?:Reference\s+re|Re)\s+[A-Z][\w'’.\-]*"
                      r"(?:\s+[\w'’.\-&()]+){0,6}?)\s*,?\s*$")


def _case_name_before(text: str, start: int) -> str | None:
    """Find the style of cause immediately preceding a citation.

    Looks back a bounded window and requires the name to sit flush against the
    citation (allowing a comma), so prose that merely happens to contain "v."
    earlier in the sentence is not captured. Returns None when there is no name
    — which is common and entirely legitimate; the model may cite a case bare."""
    # Soft line wraps inside the window are collapsed to spaces so a wrapped
    # style of cause still matches as one phrase.
    window = re.sub(r"\s*\n\s*", " ", text[max(0, start - 200) : start])
    # Never look across a sentence boundary — but a naive rfind(". ") cuts a
    # style of cause in half, because case names are full of abbreviations:
    # "... Corporation No. 590 v. Registered Owners" would be truncated at
    # "No. ", leaving a fragment that cannot match. _SENTENCE_END therefore
    # ignores a period that follows a known legal abbreviation or a single
    # initial.
    window = window[_last_sentence_start(window) :]
    m = _NAME_CORE.search(window)
    if m:
        left = _trim_left_party(m.group(1))
        if left:
            return f"{left} v. {m.group(2).strip()}"
    m = _RE_NAME.search(window)
    if m:
        return _strip_connector(m.group(1))
    return None


# Tokens that legitimately sit inside a party name despite being lowercase.
_NAME_PARTICLES = frozenset(
    "of and the for et al de la le van von der den du des à a à".split()
)


def _trim_left_party(text: str) -> str:
    """Reduce the text before " v. " to just the first party's name.

    The regex matches from the leftmost capital, which in running prose is the
    start of the SENTENCE, not the start of the case name: "Good faith was
    addressed in Wastech Services Ltd. v. Greater Vancouver" would otherwise
    yield a first party of "Good faith was addressed in Wastech Services Ltd."

    So walk right-to-left from the separator and keep tokens while they look
    like part of a name — capitalised, numeric, or a known particle — stopping
    at the first ordinary lowercase word ("was", "in", "addressed")."""
    tokens = _strip_connector(text).split()
    kept: list[str] = []
    for tok in reversed(tokens):
        bare = tok.strip("(),;:").lower()
        if tok[:1].isupper() or tok[:1].isdigit() or bare in _NAME_PARTICLES:
            kept.append(tok)
            continue
        break
    kept.reverse()
    # Drop trailing particles left dangling at the front ("of Smith v. Jones").
    while kept and kept[0].strip("(),;:").lower() in _NAME_PARTICLES:
        kept.pop(0)
    return " ".join(kept).strip()


# Words that introduce a citation rather than belong to the style of cause.
# "In Wastech Services Ltd. v. ..." must yield "Wastech Services Ltd. v. ...".
_CONNECTOR = re.compile(
    r"^(?:in|see|see\s+also|per|citing|cf\.?|accord|following|applying|"
    r"considered\s+in|approved\s+in|and|but\s+see)\s+",
    re.IGNORECASE,
)


def _strip_connector(name: str) -> str:
    return _CONNECTOR.sub("", (name or "").strip()).strip()


def _context_sentence(text: str, start: int, end: int) -> str:
    """The sentence containing the citation, for the audit log.

    This is what lets a lawyer reading the audit understand WHAT was removed and
    what proposition it was attached to — a stripped citation with no context is
    a fact about the tool, not about their document."""
    # Same abbreviation-aware boundary as case-name detection: a context
    # sentence that begins "Registered Owners, 2020 ONCA 471 ..." because it cut
    # at "No. " tells the reviewing lawyer less than the whole sentence does.
    window_start = max(0, start - 600)
    left = window_start + _last_sentence_start(text[window_start:start])
    right_candidates = [
        i for i in (text.find(". ", end), text.find("\n", end)) if i != -1
    ]
    right = min(right_candidates) + 1 if right_candidates else len(text)
    return " ".join(text[left:right].split()).strip()


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------
def extract_citations(text: str) -> list[CaseCitation]:
    """Every case citation in `text`, in order of appearance, deduped by span.

    Overlapping matches are resolved in favour of the first (leftmost) match,
    and reporter-only citations are only reported where they are not already
    part of a neutral citation's parallel tail."""
    if not text:
        return []

    taken: list[tuple[int, int]] = []

    # Matter-document citations are reserved territory.
    for m in _MATTER_CITATION.finditer(text):
        taken.append(m.span())

    def overlaps(a: int, b: int) -> bool:
        return any(a < end and start < b for start, end in taken)

    found: list[CaseCitation] = []

    # -- CanLII-series citations first: "1975 CanLII 499 (ON CA)". Run before
    # the neutral pattern because "CanLII" would otherwise match as a court
    # token and the trailing "(ON CA)" would be lost.
    for m in _CANLII.finditer(text):
        if overlaps(*m.span()):
            continue
        year, number = int(m.group(1)), int(m.group(2))
        start, end = m.span()
        end = _extend_over_tail(text, end)
        taken.append((start, end))
        found.append(
            CaseCitation(
                full_citation=f"{year} CanLII {number}"
                + (f" {m.group(3)}" if m.group(3) else ""),
                raw_text=text[start:end],
                court_token="CanLII",
                year=year,
                decision_number=number,
                span=(start, end),
                context_sentence=_context_sentence(text, start, end),
                case_name=_case_name_before(text, start),
            )
        )

    # -- Neutral citations: "2020 ONCA 471"
    for m in _NEUTRAL.finditer(text):
        if overlaps(*m.span()):
            continue
        token = m.group(2).upper()
        if token not in COURT_TOKENS:
            continue  # the whitelist is what stops prose becoming a citation
        year, number = int(m.group(1)), int(m.group(3))
        start, end = m.span()
        end = _extend_over_tail(text, end)
        taken.append((start, end))
        found.append(
            CaseCitation(
                full_citation=f"{year} {token} {number}",
                raw_text=text[start:end],
                court_token=token,
                year=year,
                decision_number=number,
                span=(start, end),
                context_sentence=_context_sentence(text, start, end),
                case_name=_case_name_before(text, start),
            )
        )

    # -- Reporter-only: "[2005] 2 S.C.R. 601". Unverifiable, but must be
    # RECOGNISED so it can be honestly marked rather than silently blessed.
    for m in _REPORTER_ONLY.finditer(text):
        if overlaps(*m.span()):
            continue
        start, end = m.span()
        taken.append((start, end))
        found.append(
            CaseCitation(
                full_citation=" ".join(text[start:end].split()),
                raw_text=text[start:end],
                court_token="",
                year=int(m.group(1)),
                decision_number=0,
                span=(start, end),
                context_sentence=_context_sentence(text, start, end),
                case_name=_case_name_before(text, start),
                supported=False,
            )
        )

    found.sort(key=lambda c: c.span[0])
    return found


def _extend_over_tail(text: str, end: int) -> int:
    """Extend a citation's span over any parallel reporter citations."""
    m = _PARALLEL_TAIL.match(text, end)
    return m.end() if m else end


def unique_citations(cites: list[CaseCitation]) -> list[CaseCitation]:
    """One representative per distinct citation, preserving order.

    Verification is per distinct citation — a case cited six times costs one
    API call, not six."""
    seen: set[str] = set()
    out: list[CaseCitation] = []
    for c in cites:
        key = normalise_citation(c.full_citation)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


# The leading neutral-citation component of a citation string, ignoring
# everything CanLII appends after it.
_NEUTRAL_CORE = re.compile(rf"^\s*{_YEAR}\s*([A-Za-z]+)\s*(\d{{1,6}})")


def normalise_citation(citation: str) -> str:
    """Comparison key identifying two renderings of the SAME case.

    Extracts the leading neutral citation and discards everything after it,
    rather than trying to strip decorations off the end. That distinction is
    load-bearing, and getting it wrong is how a real Supreme Court citation gets
    deleted from a memo:

        model wrote : "2021 SCC 7"
        CanLII gives: "2021 SCC 7 (CanLII), [2021] 1 SCR 32"

    A rule that strips only a TRAILING parenthetical leaves the parallel SCR
    citation attached, the two strings compare unequal, and a genuine SCC
    authority is reported as fabricated and stripped. Anchoring on the neutral
    core matches regardless of what CanLII appends.

    Falls back to whitespace/punctuation normalisation for reporter-only
    citations, which have no neutral core."""
    s = (citation or "").strip()
    m = _NEUTRAL_CORE.match(s)
    if m:
        return f"{m.group(1)} {m.group(2).lower()} {int(m.group(3))}"
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s)
    s = re.sub(r"[.,]", "", s)
    return " ".join(s.split()).casefold()


# --------------------------------------------------------------------------
# Case-name comparison
# --------------------------------------------------------------------------
# Deliberately LOOSE. A false "mismatch" accuses the model of misattributing a
# real case, which is a serious claim to put in front of a lawyer, so the bar
# for declaring a mismatch is: the two names share NO distinctive word at all.
# Normalisation rules, in order:
#
#   1. Case-folded; punctuation and diacritic-free apostrophes removed.
#   2. Corporate and procedural suffixes dropped (inc, ltd, corp, co, llp, ...)
#      — "Smith Holdings Inc." and "Smith Holdings Ltd." are the same party for
#      matching purposes, and CanLII's title style is not the model's.
#   3. Leading "re" / "reference re" dropped.
#   4. Party separators (v, vs, versus, c) split the name into sides, but the
#      SIDES ARE NOT COMPARED POSITIONALLY: CanLII styles some cases with the
#      parties reversed on appeal, and a positional check would flag those.
#   5. Tokens shorter than 4 characters and generic litigation words dropped,
#      leaving distinctive surnames and trade names.
#   6. Numbered companies ("1420041 Ontario Inc.") keep their digit string,
#      which is highly distinctive.
_NAME_NOISE = frozenset("""
inc inc. ltd ltd. limited corp corp. corporation co co. company llp llc plc ulc
lp holdings holding group groupe enterprises enterprise services service
canada canadian ontario the and of a an et al re reference between
appellant respondent applicant plaintiff defendant petitioner
""".split())

_SEPARATORS = re.compile(r"\s+(?:v|vs|versus|c)\.?\s+", re.IGNORECASE)


def _name_tokens(name: str) -> set[str]:
    s = (name or "").lower().replace("’", "'")
    s = re.sub(r"^\s*(?:reference\s+re|re)\s+", " ", s)
    s = _SEPARATORS.sub(" ", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    out: set[str] = set()
    for tok in s.split():
        if tok in _NAME_NOISE:
            continue
        if tok.isdigit() and len(tok) >= 5:
            out.add(tok)          # numbered company — highly distinctive
        elif len(tok) >= 4:
            out.add(tok)
    return out


def names_match(claimed: str | None, actual: str | None) -> bool:
    """True when the model's case name is compatible with CanLII's title.

    Returns True whenever no comparison is possible (no name given, or no
    usable tokens on either side) — the check exists to catch a confident
    misattribution, not to manufacture doubt."""
    if not claimed or not actual:
        return True
    a, b = _name_tokens(claimed), _name_tokens(actual)
    if not a or not b:
        return True
    return bool(a & b)
