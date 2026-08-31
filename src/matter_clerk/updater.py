r"""Update checking against GitHub Releases (Session 6a, Part B).

v1.0.0 shipped with no update mechanism, so every fix needs manual
redistribution to every lawyer. This closes that: v1.0.2 onward can reach an
installed copy on its own.

Design constraints, all of them deliberate:

  * EVERY failure is silent. Offline, proxied, rate-limited, malformed JSON,
    GitHub down -- the lawyer sees nothing and the app behaves exactly as
    before. An update checker that interrupts legal work to complain about its
    own connectivity is worse than no update checker.
  * NEVER installs without explicit confirmation.
  * The check runs on a background thread and cannot delay startup.
  * A bug in here is the hardest kind to fix remotely, because it breaks the
    mechanism you would fix it with. Hence: no clever behaviour, fail closed.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import threading
import urllib.request
from pathlib import Path

from . import __version__
from .paths import data_dir

log = logging.getLogger("matter_clerk.updater")

RELEASES_API = (
    "https://api.github.com/repos/ssamuelhui/AI-LEGAL-MATTER-CLERK/releases/latest"
)
ASSET_NAME = "MatterClerk-Setup.exe"

CHECK_TIMEOUT_SECONDS = 10
BACKOFF_SECONDS = 3600          # one hour after any failure

_STATE: dict = {"available": None, "dismissed": False}
_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# Version comparison
# --------------------------------------------------------------------------
def parse_version(text: str) -> tuple[int, ...] | None:
    """Parse 'v1.0.2', 'V1.0.0', '1.0.10' into a comparable tuple.

    Case-insensitive on the leading v because the existing release is tagged
    'V1.0.0' with a capital V -- a lexical or case-sensitive comparison would
    silently never match it. Numeric per component so v1.0.10 > v1.0.9, which
    string comparison gets backwards.
    """
    if not text:
        return None
    m = re.match(r"^\s*[vV]?(\d+(?:\.\d+)*)", str(text).strip())
    if not m:
        return None
    try:
        return tuple(int(part) for part in m.group(1).split("."))
    except ValueError:
        return None


def is_newer(candidate: str, current: str) -> bool:
    """True if `candidate` is a strictly newer version than `current`."""
    a, b = parse_version(candidate), parse_version(current)
    if a is None or b is None:
        return False
    # Zero-pad so 1.1 and 1.1.0 compare equal rather than by length.
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


# --------------------------------------------------------------------------
# Backoff bookkeeping
# --------------------------------------------------------------------------
def _state_path() -> Path:
    return data_dir() / "update_check.json"


def _load_state() -> dict:
    try:
        p = _state_path()
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:                                             # noqa: BLE001
        pass
    return {}


def _save_state(state: dict) -> None:
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:                                             # noqa: BLE001
        pass


def _in_backoff() -> bool:
    state = _load_state()
    last = state.get("last_failure_epoch")
    if not last:
        return False
    return (dt.datetime.now(dt.timezone.utc).timestamp() - float(last)) < BACKOFF_SECONDS


def _record_failure() -> None:
    state = _load_state()
    state["last_failure_epoch"] = dt.datetime.now(dt.timezone.utc).timestamp()
    _save_state(state)


# --------------------------------------------------------------------------
# The check
# --------------------------------------------------------------------------
def fetch_latest_release(url: str = RELEASES_API) -> dict | None:
    """Fetch the latest release. Returns None on any failure whatsoever."""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"MatterClerk/{__version__}",
            },
        )
        with urllib.request.urlopen(req, timeout=CHECK_TIMEOUT_SECONDS) as r:
            if r.status != 200:
                return None
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:                                        # noqa: BLE001
        log.debug(f"update check failed silently: {type(e).__name__}: {e}")
        return None


def check_for_update(current: str = __version__) -> dict | None:
    """Return update info if a newer release exists, else None.

    Shape: {version, notes, url, asset_url, asset_size}
    """
    if _in_backoff():
        return None

    data = fetch_latest_release()
    if not data:
        _record_failure()
        return None

    if data.get("draft") or data.get("prerelease"):
        return None

    tag = data.get("tag_name") or ""
    if not is_newer(tag, current):
        return None

    asset = next(
        (a for a in (data.get("assets") or []) if a.get("name") == ASSET_NAME),
        None,
    )
    if not asset:
        # A release with no installer attached cannot be installed, so there is
        # nothing to offer. Staying quiet beats an update prompt that fails.
        log.debug(f"release {tag} has no {ASSET_NAME} asset; ignoring")
        return None

    return {
        "version": tag.lstrip("vV"),
        "tag": tag,
        "notes": (data.get("body") or "").strip(),
        "url": data.get("html_url") or "",
        "asset_url": asset.get("browser_download_url") or "",
        "asset_size": int(asset.get("size") or 0),
    }


def start_background_check(current: str = __version__) -> None:
    """Kick off the check on a daemon thread. Returns immediately."""
    def worker() -> None:
        try:
            info = check_for_update(current)
            with _LOCK:
                _STATE["available"] = info
            if info:
                log.info(f"update available: {info['tag']}")
        except Exception:                                         # noqa: BLE001
            pass

    threading.Thread(target=worker, daemon=True, name="update-check").start()


def available_update() -> dict | None:
    """The pending update for the UI, or None if absent or dismissed."""
    with _LOCK:
        if _STATE["dismissed"]:
            return None
        return _STATE["available"]


def dismiss() -> None:
    """Hide the notification for this session only."""
    with _LOCK:
        _STATE["dismissed"] = True


# --------------------------------------------------------------------------
# Download + hand off to the installer
# --------------------------------------------------------------------------
def download_installer(info: dict, dest_dir: Path | None = None) -> Path:
    """Download the installer asset. Raises on failure -- the caller asked for
    this explicitly, so unlike the check it must not fail silently."""
    dest_dir = dest_dir or Path(data_dir()) / "updates"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"MatterClerk-Setup-{info['version']}.exe"

    req = urllib.request.Request(
        info["asset_url"], headers={"User-Agent": f"MatterClerk/{__version__}"}
    )
    with urllib.request.urlopen(req, timeout=120) as r, dest.open("wb") as f:
        while chunk := r.read(1024 * 256):
            f.write(chunk)

    expected = int(info.get("asset_size") or 0)
    actual = dest.stat().st_size
    if expected and actual != expected:
        dest.unlink(missing_ok=True)
        raise RuntimeError(
            f"Download incomplete ({actual:,} of {expected:,} bytes). "
            "Check your connection and try again."
        )
    return dest


def launch_installer(path: Path) -> None:
    """Start the installer detached and return so the caller can exit.

    The installer replaces files this process has open, so the caller MUST
    exit promptly afterwards.
    """
    import subprocess

    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        flags |= getattr(subprocess, name, 0)
    subprocess.Popen([str(path)], creationflags=flags, close_fds=True)
