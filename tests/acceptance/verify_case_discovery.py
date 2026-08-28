"""Verification for Phase-2a CanLII case discovery.

Two tiers, so the offline half is runnable anywhere:

    python tests/acceptance/verify_case_discovery.py          # offline only
    python tests/acceptance/verify_case_discovery.py --live   # + real CanLII

Offline covers the parts that must never regress silently: the court hierarchy
ordering (a binding authority must not rank under a persuasive one), the query
scrubbing that keeps privileged content off the wire, the rate limiter's timing
guarantee, and the truncated-JSON salvage. None of it touches the network.

--live additionally hits the real API to confirm authentication, that the
search and metadata endpoints still behave as documented in
matter_clerk.canlii, and that the CanLII URLs we hand the lawyer actually
resolve to a case.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from matter_clerk import canlii, discovery  # noqa: E402
from matter_clerk.canlii import CanLIICase, court_for  # noqa: E402

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


def mk(db: str, citation: str, year: int, *, angles: int = 1, rank: int = 0,
       keywords: str | None = None) -> CanLIICase:
    c = CanLIICase(
        database_id=db, case_id="x", title=f"Test {citation}", citation=citation,
        long_url=f"https://www.canlii.org/{db}", court=court_for(db), year=year,
    )
    c.found_by = [f"angle{i}" for i in range(angles)]
    c.best_rank = rank
    c.keywords = keywords
    return c


# --------------------------------------------------------------------------
def verify_court_hierarchy() -> None:
    """SoW 4.3.1: SCC > ONCA > Divisional > ONSC > lower > tribunal > other.

    Regression guard for a real bug: scoring the SCC as a "federal, therefore
    partial" jurisdiction match ranked a 2019 SCC decision BELOW a 2024 ONSC
    one. Binding authority under persuasive authority is a wrong answer that
    looks like a right one."""
    print("\nCourt hierarchy (SoW 4.3.1)")
    cases = [
        mk("csc-scc", "2019 SCC 1", 2019),
        mk("onca", "2021 ONCA 5", 2021),
        mk("onscdc", "2022 ONSC 9 (Div Ct)", 2022),
        mk("onsc", "2024 ONSC 1", 2024),
        mk("oncj", "2024 ONCJ 3", 2024),
        mk("oncat", "2025 ONCAT 3", 2025),
        mk("abkb", "2025 ABKB 1", 2025),
    ]
    ranked = sorted(
        cases, key=lambda c: -discovery.score_stage1(c, "Ontario", 30)[0]
    )
    order = [c.court.tier for c in ranked]
    check("tiers rank in authority order", order == sorted(order), str(order))

    scc = discovery.score_stage1(mk("csc-scc", "1997 SCC 1", 1997), "Ontario", 30)[0]
    onsc = discovery.score_stage1(mk("onsc", "2024 ONSC 1", 2024), "Ontario", 30)[0]
    check("a 1997 SCC case still outranks a 2024 ONSC case", scc > onsc,
          f"scc={scc:.3f} onsc={onsc:.3f}")

    onca = discovery.score_stage1(mk("onca", "2021 ONCA 5", 2021), "Ontario", 30)[0]
    check("ONCA outranks ONSC", onca > onsc, f"onca={onca:.3f} onsc={onsc:.3f}")

    # An Ontario tribunal must outrank an out-of-province superior court for an
    # Ontario matter; a federal court must not, on an Ontario-scoped run.
    cat = discovery.score_stage1(mk("oncat", "2025 ONCAT 3", 2025), "Ontario", 30)[0]
    ab = discovery.score_stage1(mk("abkb", "2025 ABKB 1", 2025), "Ontario", 30)[0]
    check("Ontario tribunal outranks out-of-province superior court", cat > ab,
          f"oncat={cat:.3f} abkb={ab:.3f}")

    check("SCC applications for leave are excluded outright",
          "csc-scc-al" in canlii.EXCLUDED_DATABASES)

    # The catalog omits historic courts that search still returns.
    for db in ("abqb", "skqb", "onhcj"):
        c = court_for(db)
        check(f"historic court {db} is tiered, not dumped in the default tier",
              c.tier < canlii.DEFAULT_TIER and c.recognised, f"tier={c.tier}")
    unknown = court_for("zzznotacourt")
    check("an unknown databaseId degrades rather than raising",
          unknown.tier == canlii.DEFAULT_TIER and not unknown.recognised)


def verify_subject_signal() -> None:
    """Published-but-unrelated catchwords must count AGAINST a case.

    Regression guard: with a zero (rather than negative) subject term, court
    tier and recency alone put an SCC Aboriginal-law decision and two
    automotive class actions onto a condominium-repair shortlist."""
    print("\nSubject-matter signal")
    concepts = [
        "condominium corporation duty to repair common elements",
        "Condominium Act, 1998, s. 89",
        "unit owner",
    ]
    on_point = mk("onsc", "2020 ONSC 1", 2020,
                  keywords="Property - Condominium law - Common elements - "
                           "Repair obligations - Condominium Act, 1998, s. 89")
    off_point = mk("csc-scc", "2021 SCC 28", 2021,
                   keywords="Aboriginal law - Fiduciary duties - Reserve land - "
                            "Equitable compensation")
    silent = mk("onsc", "2020 ONSC 2", 2020, keywords=None)

    check("on-point catchwords score positive",
          discovery.subject_signal(on_point, concepts) > 0)
    check("unrelated catchwords score negative",
          discovery.subject_signal(off_point, concepts) < 0)
    check("absent catchwords score neutral, not negative",
          discovery.subject_signal(silent, concepts) == 0.0)

    # The end-to-end consequence: an off-point SCC case must fall below an
    # on-point ONSC case once catchwords are in evidence.
    scc_total = (
        discovery.score_stage1(off_point, "Ontario", 30)[0]
        + discovery.W_SUBJECT * discovery.subject_signal(off_point, concepts)
    )
    onsc_total = (
        discovery.score_stage1(on_point, "Ontario", 30)[0]
        + discovery.W_SUBJECT * discovery.subject_signal(on_point, concepts)
    )
    check("off-point SCC falls below on-point ONSC after enrichment",
          onsc_total > scc_total, f"onsc={onsc_total:.3f} scc={scc_total:.3f}")


def verify_query_scrubbing() -> None:
    """Privileged matter content must not reach CanLII."""
    print("\nQuery scrubbing (confidentiality)")
    cases = [
        ("heat pump failure suite 315 unit owner", "315", "suite number"),
        ("damages of $47,500 for water escape", "47", "dollar amount"),
        ("email from raymond@example.com re repair", "@", "email address"),
        ("breach on 2026-03-27 of the declaration", "03-27", "full date"),
        ("see https://portal.example.com/file/9912", "http", "URL"),
        ("client file 2024-CV-118823 heat pump", "118823", "file number"),
    ]
    for raw, forbidden, what in cases:
        out = discovery.scrub_query(raw)
        check(f"{what} stripped", forbidden not in out, f"{raw!r} -> {out!r}")

    kept = discovery.scrub_query('"Condominium Act, 1998" section 89 repair')
    check("statute YEAR survives (it is the statute's name)", "1998" in kept, kept)
    check("section number survives", "89" in kept, kept)

    quoted = discovery.scrub_query('"common elements" suite 315 "duty to repair"')
    check("phrase quoting stays balanced after a token is dropped",
          quoted.count('"') % 2 == 0, quoted)

    wide = discovery.broaden('"duty to repair" "common elements" condominium')
    check("broaden() drops phrase quoting", '"' not in wide, wide)


def verify_rate_limiter() -> None:
    """CanLII allows 2 requests/second and 1 concurrent request.

    Drives the real gate with a stubbed transport so the timing guarantee is
    tested without spending API calls."""
    print("\nRate limiting")
    stamps: list[float] = []
    lock = threading.Lock()

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"results": []}

    def fake_get(url, params=None, timeout=None):
        with lock:
            stamps.append(time.monotonic())
        return FakeResponse()

    import requests

    original = requests.get
    requests.get = fake_get
    try:
        canlii._last_request_at = 0.0
        client = canlii.CanLIIClient(
            api_key="test-key", budget=canlii.DailyBudget(
                Path(__file__).resolve().parent / "_canlii_usage_test.json"
            )
        )
        # Eight calls from four threads at once: both the interval and the
        # 1-concurrent rule must hold under contention.
        threads = [
            threading.Thread(target=lambda: [client.search("x") for _ in range(2)])
            for _ in range(4)
        ]
        t0 = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.monotonic() - t0
    finally:
        requests.get = original
        (Path(__file__).resolve().parent / "_canlii_usage_test.json").unlink(
            missing_ok=True
        )

    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    smallest = min(gaps) if gaps else 999
    # 8 searches + exactly 1 catalog fetch. More than 9 means the catalog cache
    # raced and burned calls on a response we already had.
    check("8 searches cost exactly 9 requests (catalog fetched once)",
          len(stamps) == 9, f"{len(stamps)} requests")
    check("no two requests closer than the 0.55s interval",
          smallest >= canlii.MIN_REQUEST_INTERVAL - 0.02,
          f"smallest gap {smallest:.3f}s")
    # 2/sec means 8 calls can never complete in under ~3.85s.
    check("no burst exceeds 2 requests/second",
          all(
              sum(1 for s in stamps if t <= s < t + 1.0) <= 2
              for t in stamps
          ),
          f"elapsed {elapsed:.2f}s")
    print(f"         gaps: {', '.join(f'{g:.2f}' for g in gaps)}")


def verify_json_salvage() -> None:
    """A completion cut at the token limit must still yield what it produced."""
    print("\nTruncated-JSON salvage")
    truncated = (
        '```json\n{\n "legal_issues": ["a", "b"],\n "queries": [\n'
        '  {"angle": "doctrinal_core", "query": "one", "rationale": "r"},\n'
        '  {"angle": "statutory_hook", "query": "two", "rationale": "partial sen'
    )
    data = discovery._first_json_object(truncated)
    check("truncated mid-string still parses", data is not None)
    check("complete elements before the cut are preserved",
          data and len(data.get("queries", [])) == 2,
          str(data and len(data.get("queries", []))))
    check("well-formed fenced JSON still parses",
          discovery._first_json_object('```json\n{"a": 1}\n```') == {"a": 1})
    check("bare object parses",
          discovery._first_json_object('sure: {"a": [1,2]} done') == {"a": [1, 2]})
    check("garbage returns None",
          discovery._first_json_object("no json here at all") is None)


def verify_filters() -> None:
    print("\nHard filters")
    year = dt.date.today().year
    old = mk("onsc", f"{year - 30} ONSC 1", year - 30)
    new = mk("onsc", f"{year - 2} ONSC 1", year - 2)
    check("a case outside the date window is dropped",
          not discovery._passes_filters(old, "Ontario", year - 10))
    check("a case inside the date window is kept",
          discovery._passes_filters(new, "Ontario", year - 10))
    check("Federal scope drops provincial courts",
          not discovery._passes_filters(new, "Federal", year - 10))
    check("Ontario scope KEEPS out-of-province authority (demoted, not dropped)",
          discovery._passes_filters(mk("bcca", f"{year} BCCA 1", year),
                                    "Ontario", year - 10))
    check("citation year parses from a neutral citation",
          canlii.parse_citation_year("2020 ONCA 471 (CanLII)") == 2020)
    check("citation year parses from a pre-neutral citation",
          canlii.parse_citation_year("1975 CanLII 499 (ON CA)") == 1975)
    check("a non-year prefix yields None",
          canlii.parse_citation_year("ONCA 471") is None)


# --------------------------------------------------------------------------
def verify_live() -> None:
    """Hits the real CanLII API. Costs roughly 5 calls."""
    print("\nLIVE CanLII API")
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    try:
        client = canlii.CanLIIClient()
    except canlii.CanLIIAuthError as e:
        check("CANLII_API_KEY configured", False, str(e))
        return

    catalog = client.databases()
    check("authentication succeeds and the catalog loads", len(catalog) > 300,
          f"{len(catalog)} databases")
    check("onca is in the catalog under its real name",
          catalog.get("onca") and "Court of Appeal" in catalog["onca"].name)

    hits = client.search('"exclusive use common elements" condominium', result_count=25)
    check("search returns cases", len(hits) > 5, f"{len(hits)} cases")
    check("legislation and commentary are filtered out",
          all(h.case_id for h in hits))
    check("every hit has a parseable year",
          all(h.year for h in hits),
          str([h.citation for h in hits if not h.year][:3]))
    check("no SCC leave applications leak through",
          all(h.database_id != "csc-scc-al" for h in hits))

    top = hits[0]
    client.case_metadata(top)
    check("metadata enrichment fills the decision date",
          top.decision_date is not None, str(top.decision_date))
    check("metadata enrichment yields a short canlii.ca URL",
          top.short_url and "canlii.ca" in top.short_url, str(top.short_url))

    # The URL we hand the lawyer must be CanLII's own canonical link for this
    # case. It is verified STRUCTURALLY, not by fetching it: canlii.org sits
    # behind bot protection that returns 403 to any non-browser client
    # regardless of User-Agent (confirmed against both a default and a
    # browser-like agent). Defeating that to satisfy a test would be both
    # against CanLII's terms and pointless — the URLs are not ours to
    # construct. Both come verbatim from the API's own `url` and `longUrl`
    # fields, so the question a test can honestly answer is whether we pass
    # them through unaltered.
    import re as _re

    check("short URL is CanLII's canonical canlii.ca/t/<id> form",
          bool(_re.fullmatch(r"https://canlii\.ca/t/[a-z0-9]+", top.short_url or "")),
          str(top.short_url))
    check("long URL is a canlii.org case document URL naming this case",
          top.long_url.startswith("https://www.canlii.org/en/")
          and top.case_id in top.long_url,
          top.long_url)
    check("the URL the UI links to is the short canonical one",
          top.url == top.short_url)
    print(f"         verified live: {top.citation}")
    print(f"           short: {top.short_url}")
    print(f"           long:  {top.long_url}")
    print("         (open either in a browser; canlii.org blocks automated"
          " fetches by design)")
    print(f"         CanLII calls used by this check: {client.calls_made}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="also exercise the real CanLII API (~5 calls)")
    args = ap.parse_args()

    print("Phase-2a case discovery verification")
    verify_court_hierarchy()
    verify_subject_signal()
    verify_query_scrubbing()
    verify_rate_limiter()
    verify_json_salvage()
    verify_filters()
    if args.live:
        verify_live()

    print(f"\n{_passed} passed, {len(_failed)} failed")
    for f in _failed:
        print(f"  FAIL: {f}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
