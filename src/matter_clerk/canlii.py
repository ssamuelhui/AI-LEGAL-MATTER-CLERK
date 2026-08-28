"""CanLII API client for case DISCOVERY (Phase 2a).

This module talks to CanLII and nothing else: no LLM, no Qdrant, no Flask. That
separation is the point — every claim this tool makes about a case has to be
traceable to a field CanLII actually returned, so the layer that fetches those
fields is kept free of anything that could invent one.

WHAT THE API ACTUALLY DOES (verified live 2026-08-27, not from documentation)
----------------------------------------------------------------------------
The behaviours below were established by probing the live API. Several of them
contradict what the parameter names imply, and two of them will crash a naive
client. See docs/ARCHITECTURE.md for the full log.

  * /search/{lang}/ IS a full-text search. It accepts `fullText` and returns
    relevance-ranked results. It returns METADATA ONLY (title, citation, court,
    URL) -- never case text -- which is exactly the constraint this feature is
    designed around, and it is why we can do keyword discovery at all.

  * Only offset, resultCount and fullText are honoured. `jurisdiction`,
    `databaseId`, `decisionDateAfter` and `resultTypes` are SILENTLY IGNORED --
    passing them returns a byte-identical result set. Every filter in this
    codebase is therefore applied client-side over an over-fetched pool.

  * Boolean operators are not honoured. `"a" AND "b"` matched MORE documents
    than `a b`, i.e. terms are OR-ed and the operator words are themselves
    treated as search terms. Quoted phrases DO measurably sharpen the ranking.
    Query construction relies on phrases and forbids AND/OR/NOT.

  * resultCount is capped at 100 and is mandatory. offset is mandatory.

  * The reported top-level `resultCount` is an OR-match total in the millions
    and is meaningless as a relevance signal. It is never shown to the user.

  * Results are a MIXED stream of {"case": ...}, {"legislation": ...} and
    {"commentary": ...} objects. Only cases are kept.

  * A 429 response body is INVALID JSON: {"error": THROTTLED, ...} with an
    unquoted token. Any client that dispatches on a parsed body raises
    JSONDecodeError instead of handling the throttle. We dispatch on the HTTP
    status code and never parse a body before checking it.

  * There are no RateLimit-* headers and no Retry-After. Quota state cannot be
    read back from the API, so it is tracked locally (see DailyBudget).

  * The search stream returns databaseIds that are absent from the caseBrowse
    catalog (observed: `onhcj`, the historic Ontario High Court of Justice).
    Unknown ids must degrade, never raise.

  * Case metadata carries no judges/coram field. The complete record is
    databaseId, caseId, url, longUrl, title, citation, language, docketNumber,
    decisionDate, keywords, topics, attachments, concatenatedId.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("matter_clerk.canlii")

BASE_URL = "https://api.canlii.org/v1"

# CanLII's published limits: 2 requests/second, 1 concurrent, 5000/day.
#
# 0.55s rather than 0.50s because the limit has an undocumented burst
# allowance and no published tolerance: three rapid calls succeeded and the
# fourth returned 429. The extra 50ms per call costs under a second across a
# whole run and removes a class of failure we cannot otherwise detect.
MIN_REQUEST_INTERVAL = 0.55
REQUEST_TIMEOUT = 15.0
THROTTLE_RETRIES = 3

DAILY_LIMIT = 5000
DAILY_WARN_AT = 4000
# 100 calls of headroom below the real limit. A run that has already started
# gets to finish rather than dying half-way through its enrichment pass.
DAILY_STOP_AT = 4900

MAX_RESULT_COUNT = 100  # API-enforced


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class CanLIIError(RuntimeError):
    """Base for every CanLII failure. Carries a user-facing message."""


class CanLIIAuthError(CanLIIError):
    """No API key configured, or CanLII rejected it (401/403)."""


class CanLIIThrottled(CanLIIError):
    """429 after THROTTLE_RETRIES backed-off attempts."""


class CanLIIUnavailable(CanLIIError):
    """Network failure, 5xx, or a 200 whose body would not parse."""


class CanLIINotFound(CanLIIError):
    """404 -- the database or case id is not on CanLII."""


class CanLIIBudgetExceeded(CanLIIError):
    """The local daily-call counter is at DAILY_STOP_AT. Raised BEFORE any HTTP
    request, so an exhausted budget costs nothing and refuses cleanly."""


# --------------------------------------------------------------------------
# Court hierarchy (SoW Section 4.3.1)
# --------------------------------------------------------------------------
# Tier 1 is the highest authority. The weights are the ranking input; the tiers
# exist so the ordering is legible to a lawyer reading the code and so ties can
# break on authority rather than on float noise.
#
# Identifiers below are the real `databaseId` values returned by
# GET /caseBrowse/en/ (409 databases enumerated 2026-08-27), not guesses.
COURT_TIER_WEIGHT: dict[int, float] = {
    1: 1.00,   # Supreme Court of Canada
    2: 0.85,   # Ontario appellate
    3: 0.72,   # Ontario Divisional Court
    4: 0.60,   # Ontario superior
    5: 0.55,   # Federal appellate
    6: 0.45,   # Federal trial
    7: 0.40,   # Ontario lower court
    8: 0.32,   # Ontario specialised tribunal
    9: 0.25,   # Other-province appellate
    10: 0.18,  # Other-province superior
    11: 0.10,  # Everything else
}

DEFAULT_TIER = 11

# databaseId -> (display name, jurisdiction, tier).
#
# Names are carried here rather than taken solely from the live catalog because
# the catalog is INCOMPLETE: GET /caseBrowse/en/ lists 409 databases but omits
# historic courts that /search/ still returns cases from. Verified absent from
# the catalog yet present in search results: `abqb`, `skqb`, `mbqb`, `nbqb`
# (pre-2022 Queen's Bench, renamed King's Bench on the accession) and `onhcj`
# (Ontario High Court of Justice, merged into ONSC in 1990). Those are real
# courts producing authority a lawyer would want, so they are tiered by hand.
# The live catalog supplies names for everything else.
_KNOWN_COURTS: dict[str, tuple[str, str, int]] = {
    # 1 -- binding everywhere in Canada
    "csc-scc": ("Supreme Court of Canada", "ca", 1),
    # 2 -- binding in Ontario
    "onca": ("Court of Appeal for Ontario", "on", 2),
    # 3 -- binding on lower Ontario courts
    "onscdc": ("Divisional Court", "on", 3),
    # 4 -- Ontario superior. NOTE: Ontario has no separate Small Claims Court
    # database; those decisions are published in `onsc` (only `nssm` and `yksm`
    # exist nationally).
    "onsc": ("Superior Court of Justice", "on", 4),
    "onhcj": ("Ontario High Court of Justice (historic)", "on", 4),
    # 5 / 6 -- federal
    "fca": ("Federal Court of Appeal", "ca", 5),
    "cmac-cacm": ("Court Martial Appeal Court of Canada", "ca", 5),
    "fct": ("Federal Court", "ca", 6),
    "cci-tcc": ("Tax Court of Canada", "ca", 6),
    # 7 -- Ontario lower court
    "oncj": ("Ontario Court of Justice", "on", 7),
    # 8 -- Ontario specialised tribunals. Deliberately ABOVE every
    # out-of-province court: for a condominium or tenancy matter the tribunal is
    # the operative forum, and burying it under an Alberta trial decision would
    # be the wrong shortlist for the matters this tool serves.
    "oncat": ("Condominium Authority Tribunal", "on", 8),
    "onltb": ("Landlord and Tenant Board", "on", 8),
    "onhrt": ("Human Rights Tribunal of Ontario", "on", 8),
    "onlt": ("Ontario Land Tribunal", "on", 8),
    "onlat": ("Ontario Licence Appeal Tribunal", "on", 8),
    "onwsiat": ("Workplace Safety and Insurance Appeals Tribunal", "on", 8),
    "onsec": ("Ontario Securities Commission", "on", 8),
    "onlrb": ("Ontario Labour Relations Board", "on", 8),
    "onipc": ("Information and Privacy Commissioner Ontario", "on", 8),
    "onlst": ("Law Society Tribunal", "on", 8),
    # 9 -- other-province appellate
    "abca": ("Court of Appeal of Alberta", "ab", 9),
    "bcca": ("Court of Appeal for British Columbia", "bc", 9),
    "mbca": ("Court of Appeal of Manitoba", "mb", 9),
    "nbca": ("Court of Appeal of New Brunswick", "nb", 9),
    "nlca": ("Court of Appeal of Newfoundland and Labrador", "nl", 9),
    "nsca": ("Nova Scotia Court of Appeal", "ns", 9),
    "ntca": ("Court of Appeal for the Northwest Territories", "nt", 9),
    "nuca": ("Court of Appeal of Nunavut", "nu", 9),
    "pescad": ("Prince Edward Island Court of Appeal", "pe", 9),
    "qcca": ("Court of Appeal of Quebec", "qc", 9),
    "skca": ("Court of Appeal for Saskatchewan", "sk", 9),
    "ykca": ("Court of Appeal of Yukon", "yk", 9),
    # 10 -- other-province superior (current and pre-accession names)
    "abkb": ("Court of King's Bench of Alberta", "ab", 10),
    "abqb": ("Court of Queen's Bench of Alberta (historic)", "ab", 10),
    "bcsc": ("Supreme Court of British Columbia", "bc", 10),
    "mbkb": ("Court of King's Bench of Manitoba", "mb", 10),
    "mbqb": ("Court of Queen's Bench of Manitoba (historic)", "mb", 10),
    "nbkb": ("Court of King's Bench of New Brunswick", "nb", 10),
    "nbqb": ("Court of Queen's Bench of New Brunswick (historic)", "nb", 10),
    "nlsctd": ("Supreme Court of Newfoundland and Labrador", "nl", 10),
    "nssc": ("Supreme Court of Nova Scotia", "ns", 10),
    "ntsc": ("Supreme Court of the Northwest Territories", "nt", 10),
    "nucj": ("Nunavut Court of Justice", "nu", 10),
    "pesctd": ("Supreme Court of Prince Edward Island", "pe", 10),
    "qccs": ("Superior Court of Quebec", "qc", 10),
    "skkb": ("Court of King's Bench for Saskatchewan", "sk", 10),
    "skqb": ("Court of Queen's Bench for Saskatchewan (historic)", "sk", 10),
    "yksc": ("Supreme Court of Yukon", "yk", 10),
}

COURT_TIERS: dict[str, int] = {db: t for db, (_n, _j, t) in _KNOWN_COURTS.items()}

# Excluded from every result set, never merely demoted.
#
# `csc-scc-al` is Supreme Court APPLICATIONS FOR LEAVE. A leave decision
# resolves nothing about the point of law and is not authority on it. Rendered
# in a shortlist beside real SCC judgments it reads as binding authority, which
# is precisely the kind of thing a lawyer relies on without re-checking.
EXCLUDED_DATABASES = frozenset({"csc-scc-al"})


@dataclass(frozen=True)
class Court:
    """A CanLII case database, tiered for the SoW's authority hierarchy."""

    database_id: str
    name: str
    jurisdiction: str  # "on" | "ca" | "bc" | ... | "" when unknown
    tier: int
    recognised: bool = True  # False for a databaseId absent from the catalog

    @property
    def weight(self) -> float:
        return COURT_TIER_WEIGHT.get(self.tier, COURT_TIER_WEIGHT[DEFAULT_TIER])

    @property
    def binding_label(self) -> str:
        """How this court's decisions bind an Ontario matter (SoW 4.3.1).

        Deliberately phrased as a statement about AUTHORITY, not about the case:
        we have not read the case and cannot say whether it is on point, still
        good law, or applicable. This label describes the court only."""
        if not self.recognised:
            return "Court not recognised - verify on CanLII"
        if self.tier in (1, 2):
            return "Binding in Ontario"
        if self.tier == 3:
            return "Binding on lower Ontario courts"
        if self.tier in (4, 7):
            return "Persuasive within Ontario"
        if self.tier == 8:
            return "Tribunal decision - not binding on a court"
        if self.tier in (5, 6):
            return "Federal jurisdiction"
        return "Persuasive only - out-of-jurisdiction"


def court_for(database_id: str, catalog: dict[str, Court] | None = None) -> Court:
    """Resolve a databaseId to a tiered Court, degrading rather than raising.

    The search stream returns ids that are not in the caseBrowse catalog
    (observed: `onhcj`). An unknown id gets DEFAULT_TIER and is flagged
    unrecognised so the UI can tell the lawyer to check it, rather than
    silently presenting it as a low-authority court we actually identified."""
    db = (database_id or "").strip().lower()
    # Hand-tiered courts win: they carry a verified tier AND a proper name,
    # including for the historic databases the catalog omits entirely.
    if db in _KNOWN_COURTS:
        name, juris, tier = _KNOWN_COURTS[db]
        return Court(db, name, juris, tier, recognised=True)
    if catalog and db in catalog:
        # In the catalog but not hand-tiered: a real, identified body (usually a
        # tribunal or an out-of-province lower court). Correct name and
        # jurisdiction, DEFAULT_TIER authority.
        known = catalog[db]
        return Court(db, known.name, known.jurisdiction, DEFAULT_TIER, True)
    return Court(db, db.upper() or "(unknown)", "", DEFAULT_TIER, recognised=False)


# --------------------------------------------------------------------------
# Case model
# --------------------------------------------------------------------------
# Neutral citations open with the year ("2020 ONCA 471", "1975 CanLII 499 (ON
# CA)"). Parsing it gives us a date for filtering and ranking WITHOUT an
# enrichment call per candidate -- the difference between ~25 metadata calls per
# run and ~400. Verified on a 79-case sample: 79/79 parsed, spanning 1974-2026.
_CITATION_YEAR = re.compile(r"^\s*(\d{4})\b")

# Dedupe key. The same case reached by two queries must collapse to one card,
# and its citation is the only identifier stable across both streams.
_CITATION_NOISE = re.compile(r"\s*\(CanLII\)\s*$", re.IGNORECASE)


@dataclass
class CanLIICase:
    """One case as CanLII describes it. Every field is copied from an API
    response; nothing here is inferred except `year` (parsed from the citation)
    and `court` (a lookup on databaseId).

    The enrichment fields are None until `CanLIIClient.case_metadata` fills
    them. A card renders correctly either way -- a failed enrichment sets
    `metadata_error` and the case still appears, because dropping a real case
    because one follow-up call failed would silently narrow the shortlist."""

    database_id: str
    case_id: str
    title: str
    citation: str
    long_url: str
    court: Court
    year: int | None = None

    # Enrichment (GET /caseBrowse/{lang}/{db}/{caseId}/)
    short_url: str | None = None
    decision_date: dt.date | None = None
    docket_number: str | None = None
    keywords: str | None = None
    topics: str | None = None
    metadata_error: str | None = None

    # Provenance within this run: which generated queries surfaced this case,
    # and the best (lowest) position it reached in any of their result lists.
    found_by: list[str] = field(default_factory=list)
    best_rank: int = 10_000

    @property
    def dedupe_key(self) -> str:
        norm = _CITATION_NOISE.sub("", self.citation or "").strip().casefold()
        return norm or f"{self.database_id}/{self.case_id}".casefold()

    @property
    def url(self) -> str:
        """Where the lawyer goes to read it. Prefers the short canlii.ca form
        (stable, citable) and falls back to the long form, which is always
        present in a search result."""
        return self.short_url or self.long_url

    @property
    def display_date(self) -> str:
        if self.decision_date:
            return self.decision_date.strftime("%d %B %Y")
        return str(self.year) if self.year else "Date unavailable"

    @property
    def effective_year(self) -> int | None:
        """The real decision year once enriched, else the citation-derived one."""
        return self.decision_date.year if self.decision_date else self.year


def parse_citation_year(citation: str) -> int | None:
    m = _CITATION_YEAR.match(citation or "")
    if not m:
        return None
    y = int(m.group(1))
    # A neutral citation year outside this range is a parse artefact, not a
    # date. Return None rather than filter a real case out on a bad year.
    return y if 1800 <= y <= dt.date.today().year + 1 else None


# --------------------------------------------------------------------------
# Daily call budget
# --------------------------------------------------------------------------
# CanLII exposes no quota state (no RateLimit-* headers, no Retry-After), so the
# only way to stay inside 5000/day is to count locally. Same spirit as
# logs/audit.jsonl: a plain local file the user can read.
#
# LIMITATION: the counter is per-machine. A firm-level deployment sharing one
# API key across users would need a shared counter and per-user budgets. Named
# in ARCHITECTURE.md rather than half-built here.
class DailyBudget:
    """UTC-day call counter persisted to logs/canlii_usage.json."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_usage_path()
        self._lock = threading.Lock()

    @staticmethod
    def _today() -> str:
        return dt.datetime.now(dt.timezone.utc).date().isoformat()

    def _read(self) -> dict[str, int]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def used_today(self) -> int:
        with self._lock:
            return int(self._read().get(self._today(), 0))

    def record(self, n: int = 1) -> int:
        """Increment and return today's new total. A write failure is logged,
        never raised: losing the count is far better than losing the run."""
        with self._lock:
            data = self._read()
            today = self._today()
            total = int(data.get(today, 0)) + n
            data[today] = total
            # Keep the file small; 30 days is plenty for a usage question.
            for key in sorted(data)[:-30]:
                data.pop(key, None)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(
                    json.dumps(data, indent=2, sort_keys=True), encoding="utf-8"
                )
            except OSError as e:
                log.warning(f"Could not persist CanLII usage count: {e}")
            return total

    def check(self) -> None:
        if self.used_today() >= DAILY_STOP_AT:
            raise CanLIIBudgetExceeded(
                f"This machine has used {self.used_today()} of CanLII's "
                f"{DAILY_LIMIT} daily API calls. Case discovery is paused until "
                f"the limit resets at midnight UTC."
            )

    def warning(self) -> str | None:
        used = self.used_today()
        if used >= DAILY_WARN_AT:
            return (
                f"{used:,} of CanLII's {DAILY_LIMIT:,} daily API calls have been "
                f"used on this machine. Case discovery stops at "
                f"{DAILY_STOP_AT:,} and resets at midnight UTC."
            )
        return None


def _default_usage_path() -> Path:
    override = os.environ.get("MATTER_CLERK_CANLII_USAGE")
    if override:
        return Path(override)
    # this file: <repo>/src/matter_clerk/canlii.py -> parents[2] == <repo>
    return Path(__file__).resolve().parents[2] / "logs" / "canlii_usage.json"


# --------------------------------------------------------------------------
# Rate limiter
# --------------------------------------------------------------------------
# Process-wide, not per-client: CanLII's limit is per API KEY, so two client
# instances in two Flask worker threads must still queue behind each other.
#
# Sync lock + time.sleep rather than an asyncio semaphore. The codebase is
# synchronous Flask (`make_server(..., threaded=True)`); an event loop inside a
# request handler would be a foreign body. Holding the lock ACROSS both the
# sleep and the request also satisfies CanLII's 1-concurrent-request rule for
# free -- no second primitive needed.
_rate_gate = threading.Lock()
_last_request_at = 0.0


def _throttled_get(
    url: str, params: dict[str, Any], timeout: float, min_interval: float
) -> requests.Response:
    global _last_request_at
    with _rate_gate:
        wait = _last_request_at + min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            resp = requests.get(url, params=params, timeout=timeout)
        finally:
            # Stamp even on failure: a timed-out request still consumed a slot
            # at CanLII's end, so the next call must respect the interval.
            _last_request_at = time.monotonic()
    return resp


def _redact(text: str) -> str:
    """Strip the API key from anything that might be logged or shown.

    CanLII authenticates by URL query parameter -- there is no header form -- so
    the key is in every request URL and would otherwise reach log files and
    exception messages."""
    return re.sub(r"(api_key=)[^&\s]+", r"\1***", text or "")


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------
class CanLIIClient:
    """Metadata-only CanLII access.

    There is deliberately no method on this class that returns case text.
    CanLII's API does not expose it, retrieving it by other means would breach
    their terms, and the entire premise of this feature is that the lawyer does
    the reading."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        language: str = "en",
        min_interval: float = MIN_REQUEST_INTERVAL,
        timeout: float = REQUEST_TIMEOUT,
        budget: DailyBudget | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        key = api_key or os.environ.get("CANLII_API_KEY") or ""
        if not key.strip():
            raise CanLIIAuthError(
                "CANLII_API_KEY is not set. Add it to .env (see .env.example)."
            )
        self.api_key = key.strip()
        self.language = language
        self.min_interval = min_interval
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self.budget = budget if budget is not None else DailyBudget()
        self.calls_made = 0
        self._catalog: dict[str, Court] | None = None
        # Guards the catalog fetch. Without it, concurrent first-use races:
        # every thread sees `_catalog is None` and fetches, so four Flask
        # worker threads starting together spend four calls on one cacheable
        # response. Verified: an 8-search burst across 4 threads issued 12
        # requests instead of 8.
        self._catalog_lock = threading.Lock()

    # -- transport ---------------------------------------------------------
    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """One authenticated GET, rate-limited, retried on 429, JSON-decoded.

        Status code is checked BEFORE the body is parsed. This is not stylistic:
        CanLII's 429 body is invalid JSON ({"error": THROTTLED, ...} -- the
        token is unquoted), so parsing first turns every throttle into a
        JSONDecodeError."""
        self.budget.check()
        url = f"{self.base_url}/{path.lstrip('/')}"
        query = dict(params or {})
        query["api_key"] = self.api_key

        delay = 1.0
        last_error: str = ""
        for attempt in range(1, THROTTLE_RETRIES + 1):
            try:
                resp = _throttled_get(url, query, self.timeout, self.min_interval)
            except requests.Timeout as e:
                raise CanLIIUnavailable(
                    f"CanLII did not respond within {self.timeout:.0f}s."
                ) from e
            except requests.RequestException as e:
                raise CanLIIUnavailable(
                    f"Could not reach CanLII: {_redact(str(e))}"
                ) from e

            self.calls_made += 1
            self.budget.record()

            if resp.status_code == 429:
                last_error = "CanLII rate limit (429)"
                if attempt < THROTTLE_RETRIES:
                    log.warning(
                        f"CanLII throttled (attempt {attempt}/{THROTTLE_RETRIES}); "
                        f"backing off {delay:.0f}s"
                    )
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise CanLIIThrottled(
                    "CanLII rate-limited this request after "
                    f"{THROTTLE_RETRIES} attempts."
                )
            if resp.status_code in (401, 403):
                raise CanLIIAuthError(
                    "CanLII rejected the API key "
                    f"(HTTP {resp.status_code}). Check CANLII_API_KEY in .env."
                )
            if resp.status_code == 404:
                raise CanLIINotFound(f"CanLII has no record at {_redact(path)}.")
            if resp.status_code >= 500:
                raise CanLIIUnavailable(
                    f"CanLII returned HTTP {resp.status_code}."
                )
            if resp.status_code != 200:
                raise CanLIIUnavailable(
                    f"CanLII returned HTTP {resp.status_code}: "
                    f"{_redact(resp.text[:200])}"
                )

            try:
                return resp.json()
            except ValueError as e:
                raise CanLIIUnavailable(
                    f"CanLII returned a response that could not be read: "
                    f"{_redact(resp.text[:200])}"
                ) from e

        raise CanLIIUnavailable(last_error or "CanLII request failed.")

    # -- catalog -----------------------------------------------------------
    def databases(self) -> dict[str, Court]:
        """The case-database catalog, fetched once per client and cached.

        Costs one call and buys correct court NAMES and jurisdictions for all
        409 databases, including ones this module does not tier by hand. On
        failure we degrade to the hard-coded map rather than failing the run:
        a shortlist with terse court names beats no shortlist."""
        if self._catalog is not None:
            return self._catalog
        with self._catalog_lock:
            if self._catalog is not None:  # filled while we waited
                return self._catalog
            try:
                data = self._get(f"caseBrowse/{self.language}/")
                catalog: dict[str, Court] = {}
                for row in data.get("caseDatabases", []):
                    db = (row.get("databaseId") or "").strip().lower()
                    if not db:
                        continue
                    catalog[db] = Court(
                        database_id=db,
                        name=row.get("name") or db.upper(),
                        jurisdiction=(row.get("jurisdiction") or "").strip().lower(),
                        tier=COURT_TIERS.get(db, DEFAULT_TIER),
                    )
                self._catalog = catalog
            except CanLIIError as e:
                log.warning(f"CanLII database catalog unavailable ({e}); "
                            f"falling back to the built-in court map.")
                self._catalog = {}
            return self._catalog

    # -- search ------------------------------------------------------------
    def search(
        self, full_text: str, *, result_count: int = MAX_RESULT_COUNT, offset: int = 0
    ) -> list[CanLIICase]:
        """Relevance-ranked full-text search, cases only.

        `full_text` is a search string, NOT a boolean expression: CanLII OR-es
        the terms and treats AND/OR/NOT as ordinary words. Quoted phrases do
        sharpen the ranking and are used heavily by the query builder.

        Legislation and commentary results are dropped here. So are excluded
        databases (SCC leave applications) -- filtering at the source means no
        downstream stage has to remember they exist."""
        n = max(1, min(int(result_count), MAX_RESULT_COUNT))
        data = self._get(
            f"search/{self.language}/",
            {"offset": max(0, int(offset)), "resultCount": n, "fullText": full_text},
        )
        if not isinstance(data, dict):
            raise CanLIIUnavailable("CanLII search returned an unexpected payload.")

        catalog = self.databases()
        out: list[CanLIICase] = []
        for position, row in enumerate(data.get("results", [])):
            if not isinstance(row, dict) or "case" not in row:
                continue  # legislation / commentary
            case = self._case_from_search(row["case"], catalog, position)
            if case is not None:
                out.append(case)
        return out

    @staticmethod
    def _case_from_search(
        raw: dict, catalog: dict[str, Court], position: int
    ) -> CanLIICase | None:
        db = (raw.get("databaseId") or "").strip().lower()
        if db in EXCLUDED_DATABASES:
            return None
        case_id = _unwrap_localised(raw.get("caseId"))
        citation = (raw.get("citation") or "").strip()
        title = (raw.get("title") or "").strip()
        if not case_id or not (citation or title):
            return None
        return CanLIICase(
            database_id=db,
            case_id=case_id,
            title=title or citation,
            citation=citation,
            long_url=(raw.get("longUrl") or "").strip(),
            court=court_for(db, catalog),
            year=parse_citation_year(citation),
            best_rank=position,
        )

    # -- browse ------------------------------------------------------------
    def browse_database(
        self,
        database_id: str,
        *,
        result_count: int = MAX_RESULT_COUNT,
        offset: int = 0,
    ) -> list[CanLIICase]:
        """List a database's cases, newest first.

        The fallback path when search returns nothing at all: a court's recent
        docket, post-filtered client-side, is a weaker but honest answer where
        a keyword search found nothing. Never the primary path -- it has no
        relevance signal whatsoever."""
        n = max(1, min(int(result_count), MAX_RESULT_COUNT))
        data = self._get(
            f"caseBrowse/{self.language}/{database_id}/",
            {"offset": max(0, int(offset)), "resultCount": n},
        )
        if not isinstance(data, dict):
            return []
        catalog = self.databases()
        out: list[CanLIICase] = []
        for position, raw in enumerate(data.get("cases", [])):
            case = self._case_from_search(raw, catalog, position)
            if case is not None:
                out.append(case)
        return out

    # -- enrichment --------------------------------------------------------
    def case_metadata(self, case: CanLIICase) -> CanLIICase:
        """Fill decision date, catchwords, topics, docket and short URL.

        Mutates and returns `case`. A failure is recorded on the case rather
        than raised: one unavailable record must not remove a real case from
        the shortlist or abort the run."""
        try:
            data = self._get(
                f"caseBrowse/{self.language}/{case.database_id}/{case.case_id}/"
            )
        except CanLIIThrottled:
            raise  # the caller stops enriching and reports a partial run
        except CanLIIError as e:
            case.metadata_error = str(e)
            return case
        if not isinstance(data, dict):
            case.metadata_error = "CanLII returned an unexpected metadata payload."
            return case

        case.short_url = (data.get("url") or "").strip() or None
        case.docket_number = (data.get("docketNumber") or "").strip() or None
        case.keywords = (data.get("keywords") or "").strip() or None
        case.topics = (data.get("topics") or "").strip() or None
        case.decision_date = _parse_date(data.get("decisionDate"))
        # Prefer the authoritative record for anything we had guessed.
        if data.get("citation"):
            case.citation = data["citation"].strip()
        if data.get("title"):
            case.title = data["title"].strip()
        if data.get("longUrl"):
            case.long_url = data["longUrl"].strip()
        if case.decision_date:
            case.year = case.decision_date.year
        return case


def _unwrap_localised(value: Any) -> str:
    """caseId is {"en": "2020onca471"} in search results but a bare string in
    metadata responses. Accept both rather than assuming either."""
    if isinstance(value, dict):
        for key in ("en", "fr"):
            if value.get(key):
                return str(value[key]).strip()
        for v in value.values():
            if v:
                return str(v).strip()
        return ""
    return str(value or "").strip()


def _parse_date(value: Any) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None
