#Requires -Version 5.1
<#
.SYNOPSIS
    Stage third-party binaries into vendor/ for the PyInstaller build.

.DESCRIPTION
    Copies the pieces of Tesseract and Poppler the application actually uses
    into vendor/, and seeds the tiktoken BPE cache. vendor/ is gitignored (it
    is ~75 MB of binaries), so this script plus the recorded versions in
    vendor/VERSIONS.txt are what make the build reproducible.

    Run from anywhere; paths resolve from the script's own location.
#>
[CmdletBinding()]
param(
    [string]$TesseractDir      = "C:\Program Files\Tesseract-OCR",
    [string]$PopplerBinDir     = "C:\poppler\poppler-26.02.0\Library\bin",
    [string]$ExpectedTesseract = "5.5.",
    [string]$ExpectedPoppler   = "26.02.0",
    [switch]$SkipVersionCheck
)

$ErrorActionPreference = "Stop"

$Root    = Split-Path -Parent $PSScriptRoot
$Vendor  = Join-Path $Root "vendor"
$Python  = Join-Path $Root ".venv\Scripts\python.exe"

Write-Host "Staging vendor binaries into $Vendor" -ForegroundColor Cyan

# --- validate sources -------------------------------------------------------
$tessExe = Join-Path $TesseractDir "tesseract.exe"
if (-not (Test-Path $tessExe)) {
    throw "Tesseract not found at $tessExe. Install the UB Mannheim build (https://github.com/UB-Mannheim/tesseract/wiki) or pass -TesseractDir."
}
$popplerExe = Join-Path $PopplerBinDir "pdftoppm.exe"
if (-not (Test-Path $popplerExe)) {
    throw "Poppler not found at $popplerExe. Download from https://github.com/oschwartz10612/poppler-windows/releases or pass -PopplerBinDir."
}

# --- version check ----------------------------------------------------------
# The pin is what buys reproducibility, since vendor/ itself is not committed.
# Both tools print their version banner to stderr. Under PowerShell 5.1 with
# $ErrorActionPreference = "Stop", redirecting a native command's stderr raises
# NativeCommandError even on exit code 0 -- so drop to Continue for the capture.
function Get-NativeFirstLine {
    # NB: not $Args -- that is a PowerShell automatic variable, and using it
    # as a parameter name silently swallows the arguments.
    param([string]$Exe, [string[]]$Arguments)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & $Exe @Arguments 2>&1
    } finally {
        $ErrorActionPreference = $prev
    }
    return ($out | ForEach-Object { $_.ToString() } | Select-Object -First 1)
}

$tessVersion    = (Get-NativeFirstLine $tessExe    @("--version")) -replace '^tesseract\s+v?', ''
$popplerVersion = (Get-NativeFirstLine $popplerExe @("-v"))        -replace '^pdftoppm version\s+', ''
$tessVersion    = $tessVersion.Trim()
$popplerVersion = $popplerVersion.Trim()

Write-Host "  tesseract : $tessVersion"
Write-Host "  poppler   : $popplerVersion"

if (-not $SkipVersionCheck) {
    if (-not $tessVersion.StartsWith($ExpectedTesseract)) {
        throw "Tesseract version mismatch: found '$tessVersion', expected '$ExpectedTesseract*'. Pass -SkipVersionCheck to override, and update vendor/VERSIONS.txt and docs/DEPLOYMENT.md if this is a deliberate upgrade."
    }
    if (-not $popplerVersion.StartsWith($ExpectedPoppler)) {
        throw "Poppler version mismatch: found '$popplerVersion', expected '$ExpectedPoppler*'. Pass -SkipVersionCheck to override, and update vendor/VERSIONS.txt and docs/DEPLOYMENT.md if this is a deliberate upgrade."
    }
}

# --- tesseract --------------------------------------------------------------
# tesseract.exe finds tessdata relative to its own location, so preserving the
# exe + tessdata/ layout means no TESSDATA_PREFIX gymnastics at run time.
# Excluded: ~25 training executables (cntraining, combine_lang_model, ...),
# doc/, and osd.traineddata (10.5 MB, only needed for --psm 0/1; pytesseract's
# image_to_string uses the default psm 3).
$vTess = Join-Path $Vendor "tesseract"
if (Test-Path $vTess) { Remove-Item $vTess -Recurse -Force }
New-Item -ItemType Directory -Path $vTess -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $vTess "tessdata") -Force | Out-Null

Copy-Item $tessExe $vTess
Copy-Item (Join-Path $TesseractDir "*.dll") $vTess
Copy-Item (Join-Path $TesseractDir "tessdata\eng.traineddata") (Join-Path $vTess "tessdata")
$configs = Join-Path $TesseractDir "tessdata\configs"
if (Test-Path $configs) {
    Copy-Item $configs (Join-Path $vTess "tessdata") -Recurse
}

# --- poppler ----------------------------------------------------------------
# pdf2image shells out to pdftoppm (rendering) and pdfinfo (page count). Both
# are required; the rest of the poppler suite is not.
$vPop = Join-Path $Vendor "poppler"
if (Test-Path $vPop) { Remove-Item $vPop -Recurse -Force }
New-Item -ItemType Directory -Path $vPop -Force | Out-Null

Copy-Item (Join-Path $PopplerBinDir "pdftoppm.exe") $vPop
Copy-Item (Join-Path $PopplerBinDir "pdfinfo.exe")  $vPop
Copy-Item (Join-Path $PopplerBinDir "*.dll")        $vPop

# --- tiktoken cache ---------------------------------------------------------
# ingest.py builds its cl100k_base encoder at module import. Unseeded, that is
# a network call to openaipublic.blob.core.windows.net -- an import-time crash
# on an offline machine.
$vTik = Join-Path $Vendor "tiktoken_cache"
if (-not (Test-Path $vTik)) { New-Item -ItemType Directory -Path $vTik -Force | Out-Null }
$env:TIKTOKEN_CACHE_DIR = $vTik
& $Python -c "import tiktoken; tiktoken.get_encoding('cl100k_base'); print('  tiktoken cl100k_base cached')"
if ($LASTEXITCODE -ne 0) { throw "Failed to seed the tiktoken cache (needs internet on this run)." }

# --- record what we staged --------------------------------------------------
@(
    "# Generated by scripts/stage_vendor.ps1 -- do not edit by hand."
    "# These are the exact third-party binary versions bundled into the build."
    "staged_utc = $((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))"
    "tesseract  = $tessVersion"
    "  source   = $TesseractDir"
    "  upstream = https://github.com/UB-Mannheim/tesseract/wiki (Authenticode-signed installer)"
    "poppler    = $popplerVersion"
    "  source   = $PopplerBinDir"
    "  upstream = https://github.com/oschwartz10612/poppler-windows/releases (unsigned)"
) | Set-Content -Path (Join-Path $Vendor "VERSIONS.txt") -Encoding utf8

$size = [math]::Round(((Get-ChildItem $Vendor -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "Vendor staged: $size MB" -ForegroundColor Green
