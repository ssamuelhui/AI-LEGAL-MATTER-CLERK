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

Result: `dist\MatterClerk\MatterClerk.exe` — about **673 MB across 2146 files**.

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
| Tesseract OCR | 5.5.3.20260724 | [UB Mannheim](https://github.com/UB-Mannheim/tesseract/wiki) | Yes (Authenticode) |
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

`vendor\` and `models\` are gitignored — together they are ~295 MB of binaries
that do not belong in git history. `vendor\VERSIONS.txt`, written by the
staging script, records exactly what was used.

| Component | Size | Purpose |
|---|---|---|
| bge-small-en-v1.5 (ONNX + tokenizer) | 128 MB | Local embeddings |
| Tesseract + Poppler + tiktoken cache | 163 MB | OCR, PDF rendering, tokenization |
| chromadb + `chromadb_rust_bindings` | 67 MB | Embedded vector store |
| onnxruntime | 40 MB | Embedding inference |
| Python runtime, Flask, exports, numpy, tkinter, etc. | ~212 MB | Everything else |
| **Total** | **673 MB** | |

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

---

## 9. Building the installer

Session 5 wraps the bundle in a per-user Windows installer. Run the bundle
build first, then:

```powershell
.\build_windows.ps1 -SkipVendor -SkipModel
.\installer\build_installer.ps1
```

Result: `installer\output\MatterClerk-Setup.exe` — **196.6 MB**, compressed
from the 673 MB bundle (29.2%). Compression is LZMA2/ultra64 with
`SolidCompression=yes`, which is what makes that ratio possible: the solid
stream deduplicates the ~90 MB of DLLs that appear twice in the bundle.

The script takes about six minutes, almost all of it compression. It finds
Inno Setup at `C:\Program Files\Inno Setup 7\ISCC.exe`, falling back to the
standard Inno 6 locations; pass `-ISCC` for anything else. Pass `-Version` to
override the `1.0.0` baked into the script.

### Prerequisites

Inno Setup 7 (or 6) from <https://jrsoftware.org/isdl.php>. The compiler prints
"Non-commercial use only", which is correct for this deployment.

---

## 10. What the installer does

| | |
|---|---|
| Installs to | `%LOCALAPPDATA%\Programs\MatterClerk` |
| Data stays in | `%LOCALAPPDATA%\MatterClerk` |
| Privileges | Per-user; **no UAC prompt, no admin rights** |
| Add/Remove Programs | Registered as "Matter Clerk 1.0.1" |
| Shortcuts | Start Menu (default on), Desktop (default off) |

Per-user is deliberate: corporate laptops routinely block admin installs, and
per-user works everywhere. `PrivilegesRequiredOverridesAllowed=commandline`
leaves the door open for a machine-wide IT rollout later without editing the
script.

The two directories are deliberately distinct, so uninstalling the application
never implies deleting a lawyer's matters.

### Screens

Welcome → Licence (terms; must accept) → Install location → Shortcut options →
Ready to install → Installing → Finished, with "Launch Matter Clerk" ticked.

The installer runs `MatterClerk.exe` plainly. The launcher itself detects a
missing `.env` and shows the first-run wizard, so one code path covers both a
fresh install and a reinstall over a configured one.

### First-run wizard

A tkinter dialog, not a web page — deliberately. The wizard must appear at the
one moment the user cannot recover if it does not, and a browser-served wizard
would depend on the shell-launch browser-open path that `docs/BACKLOG.md`
records as intermittently failing. It looks dated; it appears every time.

It collects the OpenRouter key (required) and CanLII key (optional), tests them
on a background thread so the window never freezes, and writes
`%LOCALAPPDATA%\MatterClerk\.env` with both keys plus
`MODEL=xiaomi/mimo-v2.5-pro`. The file is then locked down with `icacls` to the
current user, since it holds live credentials.

On save the dialog closes and the app starts in the same process — no
subprocess, no second console, nothing to orphan. Cancel exits without writing.

To reconfigure keys later:

```powershell
& "$env:LOCALAPPDATA\Programs\MatterClerk\MatterClerk.exe" --first-run
```

The OpenRouter key is tested against `GET /api/v1/key`, **not** `GET /models`.
`/models` is unauthenticated and returns HTTP 200 for a fabricated key —
verified during this session — so testing against it would tell a lawyer their
bad key was fine.

---

## 11. Uninstalling

Settings → Apps → Matter Clerk → Uninstall, or the entry in Add/Remove
Programs.

**Matter data is kept by default.** Before anything is removed, the uninstaller
asks whether to also delete `%LOCALAPPDATA%\MatterClerk`. "No" is the default
button on that prompt, and choosing "Yes" raises a second confirmation that
also defaults to "No". Deleting client documents and the audit log has to be
chosen twice, deliberately.

This runs in `InitializeUninstall`, before any file is touched — not as a
checkbox on the progress form, which appears only after the user has already
committed to uninstalling, where a mis-click would be unrecoverable.

Keeping the data means a later reinstall picks up every matter, the search
index, and the existing `.env` — so the wizard does not reappear.

---

## 12. Installer test plan

Verified on the build machine: compilation, installer size, the wizard gating
startup in the bundled exe, tcl/tk resources present, key validation against
both live APIs. **Not** verified here — these need a real install cycle:

| # | Test | Pass criterion |
|---|---|---|
| 1 | Run `MatterClerk-Setup.exe` | SmartScreen warns (expected, unsigned): More info → Run anyway |
| 2 | Wizard screens | Welcome, licence, location, shortcuts, ready, installing, finished |
| 3 | No UAC prompt | Never appears at any point |
| 4 | Install location | Files land in `%LOCALAPPDATA%\Programs\MatterClerk` |
| 5 | Start Menu shortcut | "Matter Clerk" present and launches |
| 6 | First-run wizard appears | tkinter dialog, before any browser or server |
| 7 | Test connections | OpenRouter passes; CanLII passes or reports optional |
| 8 | Bad key feedback | Paste a wrong key: rejected clearly, not "cannot reach" |
| 9 | Save and start | `.env` written; app starts; browser opens |
| 10 | Run a real task | Query with citations works end to end |
| 11 | OCR from installed copy | Scanned PDF OCRs via the installed vendored binaries |
| 12 | Install dir stays clean | Nothing new written under `Programs\MatterClerk` after use |
| 13 | Relaunch from shortcut | Starts directly; **no** wizard second time |
| 14 | Add/Remove Programs | "Matter Clerk 1.0.1" listed with working uninstall |
| 15 | **Uninstall, keep data** | Answer No: app gone, `%LOCALAPPDATA%\MatterClerk` intact |
| 16 | Reinstall | Matters still present, no wizard (`.env` survived) |
| 17 | **Uninstall, delete data** | Answer Yes twice: data directory gone |

Tests 15-17 are the ones worth doing slowly — 15 and 17 are the whole point of
the confirmation design, and 16 is what proves 15 actually preserved something
usable rather than just leaving a folder behind.

Close Matter Clerk before uninstalling. A running instance holds its bundled
DLLs open, and the uninstaller will report files it could not remove.

---

## 13. Releasing a new version (v1.0.1 onward)

The app version lives in **one** place: `__version__` in
`src/matter_clerk/__init__.py`. `installer/matter_clerk.iss` carries a matching
`AppVersion`, and `updater.py` reads the package value to compare against
GitHub.

Do not confuse this with `<data_dir>/version.txt`, which is the data-directory
*layout* version (`paths.DATA_DIR_VERSION`) and moves independently — it says
how the folder is arranged, not which release wrote it.

```powershell
# 1. bump both, keeping them in step
#    src\matter_clerk\__init__.py   __version__ = "1.0.2"
#    installer\matter_clerk.iss     #define AppVersion "1.0.2"

# 2. rebuild
.\build_windows.ps1 -SkipVendor -SkipModel
.\installer\build_installer.ps1

# 3. publish
#    Create a GitHub Release tagged v1.0.2 and attach
#    installer\output\MatterClerk-Setup.exe under exactly that name.
```

The asset **must** be named `MatterClerk-Setup.exe`. The updater looks for that
name and stays silent if it is absent, so a release with a differently-named
asset simply never reaches anyone.

Tags may be `v1.0.2` or `V1.0.2` — comparison is case-insensitive and numeric
per component, so `v1.0.10` correctly outranks `v1.0.9`. Drafts and
pre-releases are ignored.

The release body becomes the "What's new" text in the in-app notification, so
write its first paragraph to stand alone. See `docs/RELEASE_NOTES_v1.0.1.md`.

### How updating behaves

Checked once at startup on a background thread, offered on the matters page
only — never over a result, never mid-task. Offline, proxied, rate-limited or
malformed responses fail silently with a one-hour backoff. Nothing downloads or
installs without explicit confirmation. "Later" dismisses for the session.

To test the path without publishing, temporarily lower `__version__` and start
the app: the live `V1.0.0` release will be offered.

---

## 14. File status and recovery

Every file in a matter carries an ingest status, shown in the file list:

| Status | Searchable | Meaning |
|---|---|---|
| **Ready** | yes | Indexed normally |
| **Poor scan quality** | yes | OCR output below the quality thresholds; answers may be thin |
| **No readable text** | **no** | Nothing searchable was produced; excluded from every task |
| **Ingest failed** | **no** | Ingestion raised |

Quality thresholds are in `pipeline.assess_extraction`: under 150 characters
per page, or under an 0.85 legible-character ratio, on a document where OCR
contributed. Calibrated against the nine real matter files in this repo, which
measured 811–4,006 chars/page at 0.996–1.000 legible.

Anything not "Ready" gets a **Re-process** button, which rebuilds the index
from the copy already stored. Matters with several unreadable files offer a
bulk removal — which removes them from the matter list only; the stored
documents are left on disk deliberately.

If a task cannot read some files, the result page says so **above** the answer:
"Retrieved from 26 of 28 files…". A lawyer needs to know a result is partial
before they rely on it, not after.

---

## 15. The diagnostic report

For any install behaving oddly. Matters page → **Having trouble?** → **Create
diagnostic report**; also on the error page. Writes a timestamped JSON file
into the data directory and tells the user where.

**Contains:** app and chromadb versions, platform, whether the store and
database open, per-matter file counts, each file's ingest status, collection
document counts, and a probe classifying each collection as
`ok` / `missing` / `empty` / `unreadable`.

**Contains none of:** document text, chunk text, matter names, file names,
client names, paths that could carry a client's name, or API keys. File names
are reduced to extension and character count.

That exclusion list is the design constraint, not a side effect: a lawyer has
to be able to send the file without auditing it first, or it will not be sent.

**Reading one:** check `chromadb_version` against the build machine first, then
`summary.by_probe`. A collection probing `unreadable` while reporting a
non-zero `doc_count` is the signature behind the open root-cause item in
`docs/BACKLOG.md` — that combination has not been reproducible in the lab.

---

## 16. Startup migrations

`maintenance.run_startup_migrations()` runs in the launcher before the server,
once per install, tracked by marker files in `<data_dir>/migrations/`.

`0001_backfill_ingest_status` reconciles the matter manifest against the vector
store: any file marked queryable whose collection is missing, empty or
unreadable is demoted to "No readable text" and the user is told once, via
`<data_dir>/notices.json`.

It cannot block startup. If the store will not open the manifest is left
untouched and the marker is not written, so it retries next launch — demoting
every file in every matter because Chroma was briefly unavailable would be far
worse than the problem. To force a re-run, delete the marker file.

---

## 17. File scope and ordering (v1.0.2)

### Scope selector

Every matter-mode task renders the same control from
`src/matter_clerk/templates/_file_selector.html`, with three modes:

| Mode | Behaviour |
|---|---|
| `all` | Every queryable file. The default, and identical to pre-v1.0.2 behaviour |
| `selected` | Only the ticked files. An empty tick-list means all files |
| `single` | One file, via `run_query` on a pinned collection |

Scope resolution lives in `web._resolve_scope`, which authorizes every
submitted id against the matter before anything runs. `run_matter_query` takes
no id parameter — it already accepts a file list, so scoping is passing a
shorter one.

`compare_clauses` and `suggest_cases` keep single-file mode hidden: restricting
a cross-document comparison, or matter-wide case discovery, to one file is a
contradiction. Both still support subset selection. Enforcement is server-side
(`web.WHOLE_MATTER_TASKS`); the JS only hides the option.

Unqueryable files appear greyed out with a reason rather than being hidden.
`ocr_low_quality` files remain selectable — they are searchable, just flagged.

The result page reports scope above the answer, separately from the "Drew on"
provenance line: scope is what was chosen, provenance is what contributed.

### Ordering

`matters.parse_date_prefix()` and `matters.sort_files()`. Accepted prefixes:

```
2026-03-27 - name.pdf                      single date
2026-01-21 - 2026-03-26 - name.pdf         range, hyphen-joined
2024-04-01 to 2026-04-30 - name.pdf        range, "to"-joined
2024-03-15_name.pdf   24-03-15_name.pdf    YYYY / YY, - _ or . separators
```

Ranges sort by start date. Undated files sort alphabetically after dated ones.
Ambiguous or non-prefix dates deliberately do not parse — see the ARCHITECTURE
entry for why guessing is worse than not sorting.

Order is chosen per matter from the **Sort** control and stored in
`<data_dir>/ui_prefs.json` (not the database — no schema change, no migration).
The same order drives the file list, the scope selector, and Compare Clauses'
column order.

---

## 18. Exhaustive mode (v1.0.3)

Timeline, Summarize and Find Entities can read every chunk of every selected
file instead of a retrieved top-k. Opt-in, never automatic.

| Task | Control | Options |
|---|---|---|
| Timeline | `detail_level` | Concise / Detailed / **Exhaustive** |
| Summarize | `mode` | Standard / **Exhaustive (preview)** |
| Find Entities | `mode` | Standard / **Exhaustive (preview)** |

The first option is the default in each case and is byte-identical to v1.0.2.

### Why it was needed

`search_across_collections` merges to a **global** top-k, so standard modes send
a matter-wide total of 12-28 chunks regardless of file count — 21% of a 9-file
matter, roughly 7% of a 28-file one. Full figures in ARCHITECTURE.

### Execution

Runs execute on a background thread; state lives in `<data_dir>/runs/<id>.json`
and is polled at `/runs/<id>/status` every 1.5 s. Closing the browser does not
stop a run. A run interrupted by an app restart reports as `interrupted`.

One run per matter, enforced by `<data_dir>/runs/matter-<id>.lock`. A second
submission redirects to the run already in progress.

Cancel is a flag on disk, honoured at batch boundaries. Completed batches are
kept and the partial result says so.

### Model and cost

Exhaustive runs are pinned to `anthropic/claude-opus-4.7`, overriding `MODEL`
from `.env`, and the model is named in the pre-run dialog, the run page and the
result. **This departs from the SoW's MiMo Pro default and applies to
exhaustive runs only.**

Measured on the 9-file dev matter:

```
full matter   67 chunks   54,542 in / 16,989 out   169.5 s   $0.6974   63 rows
2-file subset  5 chunks    4,258 in /  2,287 out    23.2 s   $0.0785    7 rows
```

Pricing lives in `exhaustive.MODEL_PRICING` (fetched 2026-09-01). Token
estimates apply `CLAUDE_TOKEN_INFLATION = 1.65` because `cl100k_base` is
OpenAI's tokenizer and undercounts Anthropic billing by ~1.57x on this content.

### Batching

Single pass below `INPUT_BUDGET_TOKENS = 400,000` (~840 chunks), which covers
every realistic matter. Above it, files are packed whole into batches and the
outputs concatenated. A failed batch is logged, named on the result, and does
not abort the run.

### Deduplication

Exact repeats **within one file** are collapsed (chunk-overlap artefacts).
Cross-file duplicates are deliberately preserved — see ARCHITECTURE. The run
summary always states the count, including when it is zero.

---

## 19. Word and Excel ingestion (v1.0.4)

Four formats are accepted: `.pdf`, `.eml`, `.docx`, `.xlsx`. The list lives in
`web.SUPPORTED_SUFFIXES`, with the matching `accept` attribute in
`web.UPLOAD_ACCEPT`, so the upload validation and the file picker cannot drift.

No new dependencies — `python-docx` and `openpyxl` were already present for the
exporters.

### Word (`ingest_docx.py`)

Body XML is walked in document order (not `document.paragraphs` then
`document.tables`, which loses interleaving). Blocks are packed to ~700 tokens
without crossing a heading or table boundary.

**Tracked changes:** text is read by walking `w:t` descendants and skipping
`w:delText`, giving the all-changes-accepted view. `Paragraph.text` cannot be
used — it drops tracked *insertions*, because inserted runs are not direct
children of `<w:p>`. See ARCHITECTURE; guarded by a test.

Comments are excluded for free: they live in `word/comments.xml` and never
appear in paragraph text. Footnotes are not extracted — see BACKLOG.

Locators: `§<heading>`, `§<heading> (part 2 of 3)`, `Table 3 "caption", rows
4-9`, or `¶N` where a document has no headings.

### Excel (`ingest_xlsx.py`)

Per sheet: score the first 10 rows to find the header (it is **not** always row
1), drop columns that are empty across the whole sheet, skip empty rows, and
pack remaining rows to ~700 tokens — about 8-11 rows in practice, not the 50
that would produce 3,800-token chunks.

The header line is repeated at the top of every chunk. This is load-bearing for
Session 8's exhaustive Timeline, which needs to know which column holds dates.

Cells contribute computed values (`data_only=True`). Whether the workbook
contains formulas is recorded as audit metadata via a second read.

Locator: `sheet 'Name', rows 15-24 (cols: A | B | C | D | E ...)` with real
1-based Excel row numbers.

### Statuses

Adds `password_protected` to the Session 6a vocabulary — not queryable, and
distinct from `failed` because the remedy is specific. Encryption is detected
by the OLE2 file signature (`D0 CF 11 E0 A1 B1 1A E1`), since python-docx and
openpyxl raise the same exception for encrypted and corrupt files alike.

A workbook whose formulas have no cached values gets its own message: open and
save it in Excel.

### Regression

`verify_docx_xlsx_ingestion.py` pins SHA-256 digests of the chunk lists for all
nine test-matter PDFs, captured before Session 9. PDF and EML ingestion is
byte-identical or the suite fails.

---

## 20. Soft delete, model selection and API keys (v1.0.5)

### Soft delete

`deleted_at TEXT` on `matters` and `files`; NULL means live. Added by an
idempotent migration in `init_db()`, which runs on every `connect()` — so it
checks `PRAGMA table_info` first. `ADD COLUMN` is metadata-only; no rewrite.

30-day window. Chroma collections and stored files are preserved throughout, so
restore is instant. `/deleted` lists everything with days remaining; linked from
the matters-page footer and from Settings.

Two constraint interactions, resolved asymmetrically:

| Constraint | Behaviour |
|---|---|
| `files UNIQUE (matter_id, content_sha256)` | Re-uploading a soft-deleted file **restores** it |
| `matters.name UNIQUE` | Reusing a deleted matter's name **refuses**, pointing at `/deleted` |

Matter deletion requires typing the exact name, case-sensitive after trimming.
Checked client-side to enable the button and **again server-side** before
anything is deleted.

Permanent deletion runs on a daemon thread after the server is listening
(`maintenance.start_purge_in_background()`), 25 items per launch, wrapped so it
can never affect startup. If the vector store will not open, rows are left
alone rather than orphaning their collections.

### Model selection

`model_registry.py`. Catalogue from OpenRouter's `/api/v1/models`, cached at
`<data_dir>/model_list_cache.json` for 24 hours; a stale cache is served
immediately while a background refresh runs. Total failure degrades to three
hard-coded recommended models with a banner — never an empty picker.

```
RECOMMENDED_MODELS = xiaomi/mimo-v2.5-pro
                     anthropic/claude-opus-4.7      # DOTS, not dashes
                     anthropic/claude-sonnet-5
```

Tiers from the measured distribution across 425 models: `$` ≤ $2.25 (p50),
`$$` ≤ $17.50 (p90), `$$$` above. Re-derive if the catalogue shifts materially.

Preferences per task in `<data_dir>/user_preferences.json`. Corrupt JSON is
logged and ignored, never deleted. A preference naming a vanished model warns
once and is rewritten to the default — but a *degraded* catalogue never
triggers that, so an outage cannot discard good preferences.

The picker is a server-rendered `<select>` with search layered on by inline JS.
No CDN library: the application is offline-capable and a fetched widget would
silently fail on a disconnected laptop. With JS disabled the native dropdown
still works.

Exhaustive mode remains pinned to `anthropic/claude-opus-4.7` regardless of
selection. The result page and audit log both carry `model_requested`,
`model_used` and `model_coerced`.

### API keys

Settings → API keys, both OpenRouter and CanLII. Tested against the live
service before saving, using the first-run wizard's endpoints. Written to the
data directory's `.env` and to `os.environ` (python-dotenv does not override
already-set variables).

Masks hide length as well as value. No key material — and no key length —
reaches any log. A running task finishes on the key it started with.

---

## 21. Task cost tracking (v1.0.6)

One row per task run in `task_costs`, written whatever the outcome.

### Where the number comes from

OpenRouter's own `usage.cost`, requested via
`extra_body={"usage": {"include": True}}` in `LLMClient._call`. **Not computed
from a price table** — see ARCHITECTURE. A response without a cost field
records NULL and displays as "Unknown"; token counts are still stored.

### Where the hook lives

`llm.CostAccumulator`, in a thread-local scope opened once per run by the web
layer (`llm.start_cost_run` / `end_cost_run`, or the `cost_run` context
manager). Every `LLMClient.complete()` adds to whatever scope is open.

This is deliberate: a hook inside the client counts new call sites
automatically. `discovery` makes two calls per run, so a per-call-site hook
would have under-counted Suggest Relevant Cases by half.

Exhaustive runs open their scope inside the worker thread, since the
accumulator is thread-local.

Every path closes in a `finally`, so a run that dies before the model still
records `$0.00 · failed`.

### Schema

```
task_costs(id, timestamp, matter_id, matter_name, task_id, model_used,
           input_tokens, output_tokens, cost_usd, duration_seconds,
           was_exhaustive, status, detail, calls, source, run_id)
```

`matter_id` is **not** a foreign key and `matter_name` is denormalised, so
billing history survives Session 10's 30-day purge. Three display states: live,
`(deleted)`, `(removed)`.

`status` is `completed` / `failed` / `cancelled`. `source` is `measured` or
`backfill`.

### Pages

`/costs` — the log, filterable by matter and period, sortable by any column,
with a filtered total. `/costs.csv` exports the same view, `utf-8-sig` so Excel
opens it directly. Linked from the matters footer and Settings.

Result pages carry a cost banner above the answer with a copy-amount button.

### Backfill

`maintenance.run_cost_backfill()` replays cost-bearing `matter_query` entries
from `audit.jsonl` once, marker-guarded, streamed. Only exhaustive runs from
v1.0.3-v1.0.5 ever carried cost; zeroed entries are skipped. On the development
install this recovers one row.

### Pre-run estimates

`exhaustive.pricing_for()` sources prices from `model_registry`'s catalogue
(425 models), falling back to a three-model table then to a deliberately high
default. This replaces `MODEL_PRICING`, which mispriced everything outside its
three entries in **both** directions — see ARCHITECTURE.

CanLII spending is not tracked here; it is a separate account.
