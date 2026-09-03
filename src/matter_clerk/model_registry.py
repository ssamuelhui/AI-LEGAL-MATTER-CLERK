r"""OpenRouter model list, pricing tiers and per-task model preferences.

Session 10. Three jobs that all answer "which model should this task use?":

  available_models()   the catalogue, fetched once a day and cached on disk
  tier_for()           $ / $$ / $$$, from measured pricing, not intuition
  preferences          per-task, global across matters, in a JSON file

NOTHING HERE MAY BLOCK A PAGE LOAD. The model list is a convenience; a lawyer
with no internet must still get a working form. Every failure path falls back
to the three recommended models, which are constants and therefore always
available.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import urllib.request
from pathlib import Path

from .paths import data_dir

log = logging.getLogger("matter_clerk.model_registry")

MODELS_API = "https://openrouter.ai/api/v1/models"
FETCH_TIMEOUT_SECONDS = 10
CACHE_TTL_HOURS = 24

DEFAULT_MODEL = "xiaomi/mimo-v2.5-pro"

# Starred and pinned to the top of the picker. These are the models the tasks
# have actually been exercised against.
#
# NOTE ON IDS: OpenRouter uses DOTS, not dashes, in Anthropic version numbers.
# `anthropic/claude-opus-4-7` does not exist and 404s on every call; verified
# against the live list, which currently carries 425 models.
RECOMMENDED_MODELS = (
    "xiaomi/mimo-v2.5-pro",
    "anthropic/claude-opus-4.7",
    "anthropic/claude-sonnet-5",
)

# Pricing tier boundaries, in USD per 1M tokens (prompt + completion).
#
# Derived from the actual distribution across all 425 models rather than
# guessed: p50 = $2.25 and p90 = $17.50. So "$" is the cheaper half of the
# catalogue and "$$$" is the most expensive tenth. For orientation:
# MiMo Pro $1.30 ($), Sonnet 5 $12.00 ($$), Opus 4.7 $30.00 ($$$).
#
# If OpenRouter's catalogue shifts materially these should be re-derived, not
# nudged -- the point is that they describe a real distribution.
TIER_CHEAP_MAX = 2.25
TIER_MID_MAX = 17.50

_LOCK = threading.Lock()
_MEMO: dict | None = None


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------
def _cache_path() -> Path:
    return data_dir() / "model_list_cache.json"


def _read_cache() -> dict | None:
    try:
        p = _cache_path()
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data.get("models"):
            return None
        return data
    except Exception as e:                                        # noqa: BLE001
        log.warning(f"model cache unreadable ({type(e).__name__}); ignoring")
        return None


def _write_cache(models: list[dict]) -> None:
    try:
        p = _cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "models": models,
        }, indent=2), encoding="utf-8")
    except Exception as e:                                        # noqa: BLE001
        log.warning(f"could not write model cache: {e}")


def _cache_age_hours(fetched_at: str) -> float:
    try:
        when = dt.datetime.fromisoformat(fetched_at)
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        return (dt.datetime.now(dt.timezone.utc) - when).total_seconds() / 3600
    except Exception:                                             # noqa: BLE001
        return CACHE_TTL_HOURS + 1


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------
def _price_per_million(entry: dict) -> float:
    pricing = entry.get("pricing") or {}
    try:
        return (float(pricing.get("prompt") or 0)
                + float(pricing.get("completion") or 0)) * 1_000_000
    except (TypeError, ValueError):
        return 0.0


def tier_for(price_per_million: float) -> str:
    if price_per_million <= TIER_CHEAP_MAX:
        return "$"
    if price_per_million <= TIER_MID_MAX:
        return "$$"
    return "$$$"


def _normalise(entry: dict) -> dict:
    model_id = entry.get("id") or ""
    provider = model_id.split("/", 1)[0] if "/" in model_id else ""
    price = _price_per_million(entry)
    return {
        "id": model_id,
        "name": (entry.get("name") or model_id).strip(),
        "provider": provider,
        "price_per_million": round(price, 4),
        "tier": tier_for(price),
        "context_length": entry.get("context_length") or 0,
    }


def fetch_models() -> list[dict] | None:
    """Fetch the catalogue. Returns None on any failure, never raises."""
    try:
        req = urllib.request.Request(
            MODELS_API, headers={"User-Agent": "MatterClerk", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as r:
            if r.status != 200:
                return None
            payload = json.loads(r.read().decode("utf-8"))
    except Exception as e:                                        # noqa: BLE001
        log.debug(f"model list fetch failed: {type(e).__name__}: {e}")
        return None

    entries = payload.get("data")
    if not isinstance(entries, list) or not entries:
        return None
    models = [_normalise(e) for e in entries if e.get("id")]
    return sorted(models, key=lambda m: m["id"])


def _fallback_models() -> list[dict]:
    """The recommended three, synthesised, for when the catalogue is absent.

    Hard-coded so a disconnected laptop still gets a usable picker rather than
    an empty one. Prices are the values measured on 2026-09-02; they drive only
    the tier badge, never a charge.
    """
    known = {
        "xiaomi/mimo-v2.5-pro": ("MiMo v2.5 Pro", 1.30),
        "anthropic/claude-opus-4.7": ("Claude Opus 4.7", 30.00),
        "anthropic/claude-sonnet-5": ("Claude Sonnet 5", 12.00),
    }
    out = []
    for model_id in RECOMMENDED_MODELS:
        name, price = known.get(model_id, (model_id, 0.0))
        out.append({
            "id": model_id, "name": name,
            "provider": model_id.split("/", 1)[0],
            "price_per_million": price, "tier": tier_for(price),
            "context_length": 0,
        })
    return out


def available_models(force_refresh: bool = False) -> dict:
    """The catalogue plus how it was obtained.

    Returns {models, source, stale, age_hours, degraded}. `degraded` is True
    when only the fallback three are available, which the UI must say plainly:
    a lawyer choosing from three options should know it is not the whole list.
    """
    global _MEMO

    cache = None if force_refresh else _read_cache()
    if cache:
        age = _cache_age_hours(cache.get("fetched_at", ""))
        stale = age > CACHE_TTL_HOURS
        if stale:
            # Serve the cache now and refresh behind the page. Never block a
            # form on a network call.
            _refresh_in_background()
        return {"models": cache["models"], "source": "cache", "stale": stale,
                "age_hours": round(age, 1), "degraded": False}

    models = fetch_models()
    if models:
        _write_cache(models)
        return {"models": models, "source": "api", "stale": False,
                "age_hours": 0.0, "degraded": False}

    return {"models": _fallback_models(), "source": "fallback", "stale": False,
            "age_hours": 0.0, "degraded": True}


def _refresh_in_background() -> None:
    def worker() -> None:
        models = fetch_models()
        if models:
            _write_cache(models)
            log.info(f"model list refreshed ({len(models)} models)")

    threading.Thread(target=worker, daemon=True, name="model-refresh").start()


def model_exists(model_id: str, models: list[dict] | None = None) -> bool:
    pool = models if models is not None else available_models()["models"]
    return any(m["id"] == model_id for m in pool)


def sort_for_picker(models: list[dict]) -> list[dict]:
    """Recommended first in declared order, then everything else by id."""
    rank = {mid: i for i, mid in enumerate(RECOMMENDED_MODELS)}
    starred = sorted((m for m in models if m["id"] in rank),
                     key=lambda m: rank[m["id"]])
    rest = sorted((m for m in models if m["id"] not in rank),
                  key=lambda m: m["id"])
    for m in starred:
        m["recommended"] = True
    for m in rest:
        m["recommended"] = False
    return starred + rest


# --------------------------------------------------------------------------
# Per-task preferences
# --------------------------------------------------------------------------
def _prefs_path() -> Path:
    return data_dir() / "user_preferences.json"


def load_preferences() -> dict:
    """Per-task model choices. Never raises, never crashes on bad input.

    A corrupt file is logged and ignored, NOT deleted -- a lawyer may have
    hand-edited it and the broken original is the only evidence of what they
    meant.
    """
    p = _prefs_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:                                        # noqa: BLE001
        log.warning(
            f"user_preferences.json is not valid JSON ({type(e).__name__}); "
            "using defaults. The file has been left untouched."
        )
        return {}
    if not isinstance(data, dict):
        log.warning("user_preferences.json is not an object; using defaults")
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def save_preference(task: str, model_id: str) -> None:
    prefs = load_preferences()
    prefs[task] = model_id
    try:
        p = _prefs_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(prefs, indent=2, sort_keys=True), encoding="utf-8")
    except Exception as e:                                        # noqa: BLE001
        log.warning(f"could not save model preference: {e}")


def resolve_model(task: str, models: list[dict] | None = None) -> tuple[str, str | None]:
    """The model this task should use, plus a warning if the choice was dropped.

    Returns (model_id, warning). An unknown task falls back to the default
    silently -- there is nothing to warn about. A preference naming a model
    that no longer exists warns once and REWRITES the preference, so the
    warning does not recur on every page load.
    """
    prefs = load_preferences()
    chosen = prefs.get(task)
    if not chosen:
        return DEFAULT_MODEL, None

    pool = models if models is not None else available_models()["models"]
    # A degraded catalogue (three fallback entries) must not be treated as
    # proof that a model is gone -- that would rewrite good preferences during
    # an outage.
    if len(pool) <= len(RECOMMENDED_MODELS):
        return chosen, None

    if model_exists(chosen, pool):
        return chosen, None

    save_preference(task, DEFAULT_MODEL)
    return DEFAULT_MODEL, (
        f"The model '{chosen}' is no longer available from OpenRouter. "
        f"This task has been set back to {DEFAULT_MODEL}."
    )
