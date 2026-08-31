# Deployment — Windows packaged build

How Matter Clerk becomes a `.exe` that runs on an Ontario lawyer's laptop with
no Python, no Docker, and no internet.

Session 3 produces the **runtime**: a folder containing `MatterClerk.exe` and
its support files. Session 5 wraps that folder in an Inno Setup installer.

---

## 1. Quick start

From the project root, on Windows:

```powershell
.\.venv\Scripts\python.exe -m pip install "pyinstaller>=6.6,<7" platformdirs
.\build_windows.ps1
```

That is the whole build. It stages the vendored binaries, downloads the
embedding model if absent, runs PyInstaller, and prints the output size.

Result: `dist\MatterClerk\MatterClerk.exe` — about **542 MB across 1212 files**.

Useful flags:

```powershell
.\build_windows.ps1 -SkipVendor    # vendor\ already staged; skips re-copying
.\build_windows.ps1 -SkipModel     # models\ already downloaded
```

`build_windows.ps1` resolves every path from its own location, so it runs from
any working directory.

---

## 2. Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ virtualenv at `.venv` | The build uses `.venv\Scripts\python.exe` explicitly |
| Project dependencies installed | `.\.venv\Scripts\python.exe -m pip install -e .` |
| `pyinstaller>=6.6,<7` | 6.x for correct Windows path handling |
| `platformdirs>=4.2` | Resolves the runtime data directory |
| Tesseract OCR (Windows) | Default `C:\Program Files\Tesseract-OCR` |
| Poppler (Windows) | Default `C:\poppler\poppler-26.02.0\Library\bin` |
| Internet, first build only | To fetch the embedding model and seed the tiktoken cache |

### Third-party binaries

Pinned versions, asserted by `scripts\stage_vendor.ps1`:

| Component | Version | Source | Signed |
|---|---|---|---|
| Tesseract OCR | 5.5.0.20241111 | [UB Mannheim](https://github.com/UB-Mannheim/tesseract/wiki) | Yes (Authenticode) |
| Poppler | 26.02.0 | [oschwartz10612/poppler-windows](https://github.com/oschwartz10612/poppler-windows/releases) | **No** |

No signed Poppler build for Windows exists short of compiling it yourself. Code
signing is deferred by decision, so this changes nothing today — but Session
5's installer will ship an unsigned third-party binary, which is worth knowing
before it reaches a client machine.

If your installs live elsewhere, or you are deliberately moving to newer
versions:

```powershell
.\scripts\stage_vendor.ps1 -TesseractDir "D:\Tesseract-OCR" -PopplerBinDir "D:\poppler\Library\bin"
.\scripts\stage_vendor.ps1 -SkipVersionCheck      # deliberate version change
```

After a deliberate version change, update the pins in `scripts\stage_vendor.ps1`
and the table above, so the next build fails loudly rather than silently
shipping something different.

---

## 3. What gets bundled, and why it is that size

`vendor\` and `models\` are gitignored — together they are ~230 MB of binaries
that do not belong in git history. `vendor\VERSIONS.txt`, written by the
staging script, records exactly what was used.

| Component | Size | Purpose |
|---|---|---|
| bge-small-en-v1.5 (ONNX + tokenizer) | 128 MB | Local embeddings |
| Tesseract + Poppler + tiktoken cache | 99 MB | OCR, PDF rendering, tokenization |
| chromadb + `chromadb_rust_bindings` | 67 MB | Embedded vector store |
| onnxruntime | 40 MB | Embedding inference |
| Python runtime, Flask, exports, numpy, etc. | ~208 MB | Everything else |
| **Total** | **542 MB** | |

Everything is bundled rather than downloaded on first run. The users are
lawyers on corporate networks with HTTPS inspection, proxies, and blocked
package installs; a first-run download is a support call they have no way to
resolve.

---

## 4. Where the application writes

The bundle installs to a read-only location (Program Files, eventually), so no
user data lives inside it.

| Mode | Data directory |
|---|---|
| Packaged | `%LOCALAPPDATA%\MatterClerk` |
| Source checkout | the repo root (unchanged from Phase 1/2) |
| Either, overridden | `MATTER_CLERK_DATA_DIR` |

Contents: `matter_clerk.db`, `data\matters\`, `data\chroma\`, `logs\`,
`version.txt` (layout marker, currently `1`), and `.env` in the packaged case.

The directory is created silently on first launch.

### Configuration

The packaged app reads `.env` from its data directory —
`%LOCALAPPDATA%\MatterClerk\.env`. Until Session 5's first-run wizard exists,
create it by hand from `.env.example`:

```
OPENROUTER_API_KEY=sk-or-...
MODEL=xiaomi/mimo-v2.5-pro
CANLII_API_KEY=
```

Without it the app still launches and ingests documents; only the LLM-backed
tasks fail.

---

## 5. Running it

```powershell
.\dist\MatterClerk\MatterClerk.exe
```

Starts the server on `http://127.0.0.1:5050` and opens your browser once
`/healthz` responds. The console window stays open and is the app's log — close
it, or press Ctrl+C, to stop. In-flight requests finish before exit.

Console mode is deliberate for now: a windowed build that dies on startup gives
the user nothing to report. It becomes `console=False` in `matter_clerk.spec`
once the build has been trusted for a while.

`--no-browser` starts the server without opening a browser.

### Port already in use

```
ERROR: 127.0.0.1:5050 is already in use.
```

Almost always a second copy already running. To run on another port:

```powershell
$env:MATTER_CLERK_PORT = "5051"
.\dist\MatterClerk\MatterClerk.exe
```

Note that two instances sharing one data directory is **not** supported —
embedded ChromaDB is owned by a single process. Use `MATTER_CLERK_PORT` to
reach a stuck instance, not to run two at once.

---

## 6. Post-build test plan

Run these against `dist\MatterClerk\MatterClerk.exe`. Tests 1, 3, 4, 6 and
10-11 were verified on the build machine; the rest need your judgement on
output quality, an API key, or -- for test 2 -- a launch context the build
machine did not exercise.

| # | Test | Pass criterion |
|---|---|---|
| 1 | Launch the exe | Console banner, no traceback |
| 2 | Browser opens | `http://127.0.0.1:5050` renders within ~15 s. **Test by double-clicking the exe in Explorer, not from a terminal** -- see the known issue in §7 |
| 3 | Data directory | `%LOCALAPPDATA%\MatterClerk\` created, `version.txt` = `1` |
| 4 | Create a matter | Redirects to the matter page |
| 5 | Upload a native-text PDF | Ingests, chunk count > 0 |
| 6 | Upload a **scanned** PDF | Console logs OCR activity; blue banner lists OCR'd pages |
| 7 | Run a query | Answer cites `[file.pdf p.N]`; citations verify |
| 8 | CanLII task | Cases ranked SCC → ONCA → ONSC → other; citations verified |
| 9 | Export DOCX / PDF / XLSX | All three download, DRAFT marking intact |
| 10 | Second instance | Prints the port-in-use message, exits 1 |

### 11. Offline test — the one that proves the bundling worked

1. Disconnect from the internet (turn off Wi-Fi, unplug Ethernet).
2. Launch `MatterClerk.exe`.
3. Create a matter and upload a **scanned** PDF.
4. It must ingest: OCR runs, pages are embedded, chunks are indexed.

If any step reaches for the network, this fails — which is exactly what it is
for. Steps 7 and 8 will fail offline by design; they call an LLM and CanLII.

An equivalent check, without unplugging anything, is to blackhole outbound HTTP:

```powershell
$env:HTTP_PROXY  = "http://127.0.0.1:9"
$env:HTTPS_PROXY = "http://127.0.0.1:9"
.\dist\MatterClerk\MatterClerk.exe
```

This is what was run on the build machine: a two-page scanned PDF OCR'd and
embedded with every outbound HTTP route dead.

---

## 7. Things to watch for

**SmartScreen / antivirus.** The exe is unsigned, and unsigned PyInstaller
bundles are a known heuristic trigger. Expect "Windows protected your PC" on a
fresh machine (More info → Run anyway). Some corporate AV will quarantine it
outright. Signing is deferred; this is the cost.

**First launch is slow.** Five to ten seconds before the port opens, while the
ONNX runtime and ChromaDB import. The browser only opens once `/healthz`
answers, so this looks like a pause rather than a failure.

**First embedding call adds ~1 s** for ONNX session construction, once per
process.

**The browser may not auto-open when launched from Explorer.** Double-clicking
the exe sometimes starts the server without opening a browser, where launching
from a terminal works. The console prints the URL; navigate there manually.
Known issue, tracked in docs/BACKLOG.md, to be fixed in Session 5 -- the
installer shortcut is a shell launch, which is the context that fails.

**Moving the folder is fine; splitting it is not.** `MatterClerk.exe` needs its
`_internal\` sibling.

**Antivirus and OCR.** Some endpoint protection blocks executables that spawn
other executables. OCR shells out to `tesseract.exe` and `pdftoppm.exe` inside
`_internal\vendor\`; if OCR fails only on a managed machine, that is the first
thing to check.

---

## 8. Rebuilding after a code change

```powershell
.\build_windows.ps1 -SkipVendor -SkipModel
```

Skipping the staging and download steps takes the build to about four minutes.
Both are only needed when the binaries or the model change.
