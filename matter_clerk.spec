# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Matter Clerk (Windows, onedir).

Onedir, not onefile: onefile re-extracts the whole bundle to %TEMP% on every
launch, which at this size is a multi-hundred-megabyte write and tens of
seconds of startup, every time. Onedir also keeps tracebacks pointing at real
paths, which matters while the console is still visible.

Build with scripts/build_windows.ps1, which stages vendor/ first.
"""

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

block_cipher = None

# ---------------------------------------------------------------------------
# Data files
#
# Read-only resources the app locates through matter_clerk.paths.resource_path,
# which resolves them under sys._MEIPASS when frozen. The destination paths
# here must therefore mirror the repo-relative paths used in the source tree.
# ---------------------------------------------------------------------------
datas = [
    ("prompts/templates", "prompts/templates"),

    # Flask resolves its template folder relative to the package directory, so
    # these must land beside the frozen matter_clerk package, not at the root.
    ("src/matter_clerk/templates", "matter_clerk/templates"),

    # Not read by any code yet (Phase 4 procedure tracker), but kilobytes, and
    # shipping them now avoids a "why is the installed build missing this"
    # discovery later.
    ("config/sources.yaml", "config"),
    ("config/required_docs.yaml", "config"),

    # bge-small-en-v1.5: ONNX weights + tokenizer. Bundled rather than
    # downloaded on first run -- see docs/ARCHITECTURE.md. ~133 MB.
    ("models/bge-small-en-v1.5", "models/bge-small-en-v1.5"),

    # Third-party binaries, staged by scripts/stage_vendor.ps1. Deliberately
    # declared as datas rather than binaries: PyInstaller rewrites and
    # relocates entries in `binaries`, which would break tesseract.exe's
    # exe-relative discovery of its own tessdata/ directory.
    ("vendor/tesseract", "vendor/tesseract"),
    ("vendor/poppler", "vendor/poppler"),

    # cl100k_base BPE ranks. ingest.py builds its encoder at module import, so
    # without this the first run on an offline machine is an import crash.
    ("vendor/tiktoken_cache", "vendor/tiktoken_cache"),
]

binaries = []
hiddenimports = []

# ---------------------------------------------------------------------------
# Packages that resolve things dynamically and so defeat static analysis
# ---------------------------------------------------------------------------

# chromadb imports its own submodules by name and reads its version through
# importlib.metadata; collect_all also picks up the chromadb_rust_bindings
# native extension.
for pkg in ("chromadb", "onnxruntime", "tokenizers"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# tiktoken registers its encodings through a plugin module discovered by name.
hiddenimports += ["tiktoken_ext.openai_public", "tiktoken_ext"]

# The application's own package: web.py imports several modules lazily and
# through the `from . import (...)` tuple, which analysis handles, but
# collect_submodules is the cheap guarantee that nothing is missed.
hiddenimports += collect_submodules("matter_clerk")

# Flask's stack and the export renderers.
hiddenimports += [
    "flask", "jinja2", "werkzeug", "werkzeug.serving", "markupsafe",
    "markdown", "bleach", "yaml", "dotenv", "platformdirs",
    "docx", "reportlab", "openpyxl", "pypdf", "pdf2image", "pytesseract",
    "PIL", "PIL.Image",
]

# Several libraries look up their own distribution version at import time.
for dist in ("chromadb", "onnxruntime", "tokenizers", "tiktoken",
             "flask", "werkzeug", "jinja2", "numpy", "tqdm", "pyyaml",
             "python-docx", "reportlab", "openpyxl", "markdown", "bleach"):
    try:
        datas += copy_metadata(dist)
    except Exception:
        # A metadata lookup failing is not fatal -- it only matters for the
        # libraries that actually introspect themselves, and those are pinned
        # dependencies that will be present.
        pass

# ---------------------------------------------------------------------------
# Exclusions
#
# Phase 3 Session 3 replaced sentence-transformers/torch with ONNX Runtime.
# Both may still be present in a developer's venv; excluding them explicitly
# keeps a stale install from silently adding ~700 MB to the bundle.
# ---------------------------------------------------------------------------
excludes = [
    "torch", "torchvision", "torchaudio",
    "transformers", "sentence_transformers",
    "scipy", "sklearn", "scikit-learn",
    "matplotlib", "pandas", "notebook", "IPython", "jupyter",
    "tkinter", "test", "unittest",
]

a = Analysis(
    ["matter_clerk_launcher.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MatterClerk",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX-compressed exes are a reliable antivirus trigger
    # Session 3 ships with the console visible: a packaged Flask server that
    # dies in windowed mode leaves the user with no traceback at all. Flip to
    # False once the build is trusted (Session 5 or later).
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,          # added in the installer session
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MatterClerk",
)
