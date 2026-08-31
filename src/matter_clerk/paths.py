from __future__ import annotations

import os
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Path resolution for both source checkouts and frozen (PyInstaller) bundles.
#
# Two distinct questions live here, and conflating them is how an application
# ends up trying to write to Program Files:
#
#   resource_path(rel)  READ-ONLY things shipped with the code -- prompt
#                       templates, HTML templates, the embedding model, the
#                       vendored Tesseract/Poppler binaries.
#   data_path(rel)      WRITABLE user state -- the SQLite DB, the matter file
#                       store, the Chroma collections, the audit log.
#
# In a source checkout both resolve under the repo root, which is exactly the
# layout every module used before Phase 3 Session 3. In a bundle they diverge:
# resources come out of sys._MEIPASS (read-only, inside the install dir) and
# data goes to %LOCALAPPDATA%\MatterClerk (writable, survives reinstall).
# --------------------------------------------------------------------------

APP_NAME = "MatterClerk"

# Bumped when the on-disk layout of the data directory changes in a way that
# needs a migration step. Written to <data_dir>/version.txt on first run so a
# future release can tell "fresh install" from "written by an older version".
DATA_DIR_VERSION = "1"


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path | None:
    """The unpacked bundle root, or None in a source checkout.

    Onedir mode sets sys._MEIPASS to the `_internal` folder beside the exe;
    onefile sets it to the extraction tempdir. We only ship onedir.
    """
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else None


def repo_root() -> Path:
    # this file: <repo>/src/matter_clerk/paths.py  ->  parents[2] == <repo>
    return Path(__file__).resolve().parents[2]


def resource_path(rel: str) -> Path:
    """Locate a read-only resource shipped with the application."""
    base = bundle_root()
    if base is not None:
        return base / rel
    return repo_root() / rel


# --------------------------------------------------------------------------
# Writable application data
# --------------------------------------------------------------------------
def data_dir() -> Path:
    r"""Root for everything the application writes.

    1. MATTER_CLERK_DATA_DIR wins everywhere (tests, sysadmins, portable use).
    2. Frozen  -> %LOCALAPPDATA%\MatterClerk via platformdirs.
    3. Source  -> the repo root, unchanged from Phase 1/2. This carve-out is
       deliberate: without it, running from source after this change would
       silently abandon the developer's existing matter_clerk.db, data/chroma
       and data/matters.
    """
    override = os.environ.get("MATTER_CLERK_DATA_DIR")
    if override:
        return Path(override)
    if is_frozen():
        # Imported lazily: platformdirs is only needed in the frozen path, and
        # keeping it out of module import keeps `import matter_clerk.paths` cheap.
        from platformdirs import user_data_dir

        # appauthor=False matters. Omitted, platformdirs defaults appauthor to
        # the appname and yields ...\Local\MatterClerk\MatterClerk.
        return Path(user_data_dir(APP_NAME, appauthor=False))
    return repo_root()


def data_path(rel: str) -> Path:
    """Locate a writable path under the data directory."""
    return data_dir() / rel


def ensure_data_dir() -> Path:
    """Create the data directory tree if absent and return it.

    Silent by design -- no prompt. A first-run wizard that cannot write
    anywhere is a worse failure than a directory that simply appears, and the
    location is logged by the launcher either way.
    """
    root = data_dir()
    for sub in ("", "data", "data/matters", "logs"):
        (root / sub if sub else root).mkdir(parents=True, exist_ok=True)

    marker = root / "version.txt"
    if not marker.exists():
        marker.write_text(DATA_DIR_VERSION + "\n", encoding="utf-8")
    return root


def data_dir_version() -> str | None:
    """Layout version recorded in the data directory, or None if unwritten.

    Nothing reads this yet. It exists so a future release can distinguish a
    fresh install from one written by an older version without guessing from
    the presence or absence of individual files.
    """
    marker = data_dir() / "version.txt"
    if not marker.exists():
        return None
    return marker.read_text(encoding="utf-8").strip() or None


# --------------------------------------------------------------------------
# Bundled third-party binaries
#
# `pytesseract` and `pdf2image` shell out to tesseract.exe and pdftoppm.exe.
# Before this change both had to be on PATH. Now: an explicit override wins,
# else the vendored copy if present (bundled, or staged into vendor/ during
# development), else None -- which means "fall back to PATH", preserving the
# pre-Session-3 behaviour for a checkout that has not run the staging script.
# --------------------------------------------------------------------------
def tesseract_exe() -> Path | None:
    override = os.environ.get("MATTER_CLERK_TESSERACT_EXE")
    if override:
        return Path(override)
    candidate = resource_path("vendor/tesseract/tesseract.exe")
    return candidate if candidate.is_file() else None


def tessdata_dir() -> Path | None:
    exe = tesseract_exe()
    if exe is None:
        return None
    candidate = exe.parent / "tessdata"
    return candidate if candidate.is_dir() else None


def poppler_bin_dir() -> Path | None:
    override = os.environ.get("MATTER_CLERK_POPPLER_PATH")
    if override:
        return Path(override)
    candidate = resource_path("vendor/poppler")
    return candidate if candidate.is_dir() else None


def tiktoken_cache_dir() -> Path | None:
    """Directory holding the pre-seeded cl100k_base BPE ranks.

    tiktoken downloads this file from openaipublic.blob.core.windows.net on a
    cache miss, and `ingest.py` builds its encoder at module import -- so an
    unseeded cache is an import-time crash on an offline machine, not a
    degraded feature. The launcher points TIKTOKEN_CACHE_DIR here before any
    matter_clerk import.
    """
    override = os.environ.get("TIKTOKEN_CACHE_DIR")
    if override:
        return Path(override)
    candidate = resource_path("vendor/tiktoken_cache")
    return candidate if candidate.is_dir() else None


def embedding_model_dir() -> Path:
    """Directory holding the bundled bge-small-en-v1.5 ONNX weights + tokenizer."""
    override = os.environ.get("MATTER_CLERK_MODEL_DIR")
    if override:
        return Path(override)
    return resource_path("models/bge-small-en-v1.5")
