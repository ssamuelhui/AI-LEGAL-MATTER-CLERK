#Requires -Version 5.1
<#
.SYNOPSIS
    Build the Matter Clerk Windows bundle with PyInstaller.

.DESCRIPTION
    Runnable from anywhere -- all paths resolve from the script's own location.

    Produces dist\MatterClerk\MatterClerk.exe (onedir). Session 5's Inno Setup
    installer wraps this folder.

.EXAMPLE
    .\build_windows.ps1
    .\build_windows.ps1 -SkipVendor      # vendor/ already staged
#>
[CmdletBinding()]
param(
    [switch]$SkipVendor,
    [switch]$SkipModel
)

$ErrorActionPreference = "Stop"
$Root   = $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Spec   = Join-Path $Root "matter_clerk.spec"

Write-Host "=== Matter Clerk Windows build ===" -ForegroundColor Cyan
Write-Host "root: $Root"

if (-not (Test-Path $Python)) {
    throw "No virtualenv at $Python. Create one and `pip install -e .` first."
}

# --- prerequisites ----------------------------------------------------------
& $Python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed. Run: .\.venv\Scripts\python.exe -m pip install `"pyinstaller>=6.6,<7`""
}
& $Python -c "import platformdirs" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "platformdirs is not installed. Run: .\.venv\Scripts\python.exe -m pip install platformdirs"
}

# --- model ------------------------------------------------------------------
$ModelOnnx = Join-Path $Root "models\bge-small-en-v1.5\onnx\model.onnx"
if (-not $SkipModel) {
    if (Test-Path $ModelOnnx) {
        Write-Host "Model already present, skipping download." -ForegroundColor DarkGray
    } else {
        Write-Host "`n--- fetching embedding model ---" -ForegroundColor Cyan
        & (Join-Path $Root "scripts\fetch_model.ps1")
    }
}
if (-not (Test-Path $ModelOnnx)) {
    throw "Missing $ModelOnnx. Run scripts\fetch_model.ps1."
}

# --- vendored binaries ------------------------------------------------------
if (-not $SkipVendor) {
    Write-Host "`n--- staging vendor binaries ---" -ForegroundColor Cyan
    & (Join-Path $Root "scripts\stage_vendor.ps1")
}
foreach ($required in @("vendor\tesseract\tesseract.exe", "vendor\poppler\pdftoppm.exe", "vendor\tiktoken_cache")) {
    if (-not (Test-Path (Join-Path $Root $required))) {
        throw "Missing $required. Run scripts\stage_vendor.ps1."
    }
}

# --- clean ------------------------------------------------------------------
Write-Host "`n--- cleaning previous build ---" -ForegroundColor Cyan
foreach ($d in @("build", "dist")) {
    $p = Join-Path $Root $d
    if (Test-Path $p) { Remove-Item $p -Recurse -Force; Write-Host "  removed $d\" }
}

# --- build ------------------------------------------------------------------
Write-Host "`n--- running PyInstaller (several minutes) ---" -ForegroundColor Cyan
Push-Location $Root
# PyInstaller writes its INFO log to stderr. Under Windows PowerShell 5.1 with
# $ErrorActionPreference = "Stop", a native command's stderr is promoted to a
# terminating NativeCommandError even on exit code 0 -- which makes the build
# fail purely because of how the script was invoked (e.g. piped into
# Select-Object). Drop to Continue for the call and judge success by the exit
# code, which is the only reliable signal here.
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    & $Python -m PyInstaller --noconfirm --clean $Spec
    $code = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $prevEap
    Pop-Location
}
if ($code -ne 0) { throw "PyInstaller failed with exit code $code." }

# --- report -----------------------------------------------------------------
$Out = Join-Path $Root "dist\MatterClerk"
$Exe = Join-Path $Out "MatterClerk.exe"
if (-not (Test-Path $Exe)) { throw "Build finished but $Exe is missing." }

$bytes = (Get-ChildItem $Out -Recurse -File | Measure-Object Length -Sum).Sum
$files = (Get-ChildItem $Out -Recurse -File | Measure-Object).Count
Write-Host "`n=== build complete ===" -ForegroundColor Green
Write-Host ("  {0}" -f $Exe)
Write-Host ("  {0} MB across {1} files" -f [math]::Round($bytes / 1MB, 1), $files)
Write-Host "`nSmoke test:  .\dist\MatterClerk\MatterClerk.exe"
