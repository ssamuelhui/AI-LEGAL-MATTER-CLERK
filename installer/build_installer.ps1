#Requires -Version 5.1
<#
.SYNOPSIS
    Compile the Matter Clerk Windows installer with Inno Setup.

.DESCRIPTION
    Runs after build_windows.ps1 has produced dist\MatterClerk\. Verifies the
    bundle and Inno Setup are both present, compiles installer\matter_clerk.iss,
    and reports the resulting installer's location and size.

    Runnable from anywhere -- all paths resolve from the script's own location.

.EXAMPLE
    .\installer\build_installer.ps1
    .\installer\build_installer.ps1 -Version 1.0.1
#>
[CmdletBinding()]
param(
    [string]$Version,
    [string]$ISCC = "C:\Program Files\Inno Setup 7\ISCC.exe"
)

$ErrorActionPreference = "Stop"

$InstallerDir = $PSScriptRoot
$Root         = Split-Path -Parent $InstallerDir
$Iss          = Join-Path $InstallerDir "matter_clerk.iss"
$Bundle       = Join-Path $Root "dist\MatterClerk"
$OutDir       = Join-Path $InstallerDir "output"

Write-Host "=== Matter Clerk installer build ===" -ForegroundColor Cyan
Write-Host "root: $Root"

# --- prerequisites ----------------------------------------------------------
if (-not (Test-Path $Iss)) { throw "Missing $Iss." }

if (-not (Test-Path $ISCC)) {
    # Fall back to a couple of common locations before giving up, so a machine
    # with Inno installed somewhere else still builds without editing this file.
    $candidates = @(
        "C:\Program Files\Inno Setup 7\ISCC.exe",
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    )
    $found = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $found) {
        throw "Inno Setup compiler not found. Looked for '$ISCC' and standard install locations. Install Inno Setup from https://jrsoftware.org/isdl.php or pass -ISCC."
    }
    $ISCC = $found
}
Write-Host "iscc: $ISCC"

$Exe = Join-Path $Bundle "MatterClerk.exe"
if (-not (Test-Path $Exe)) {
    throw "No bundle at $Bundle. Run .\build_windows.ps1 first."
}

$bundleBytes = (Get-ChildItem $Bundle -Recurse -File | Measure-Object Length -Sum).Sum
$bundleFiles = (Get-ChildItem $Bundle -Recurse -File | Measure-Object).Count
Write-Host ("bundle: {0} MB across {1} files" -f [math]::Round($bundleBytes / 1MB, 1), $bundleFiles)

# --- compile ----------------------------------------------------------------
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }

$isccArgs = @()
if ($Version) { $isccArgs += "/DAppVersion=$Version" }
$isccArgs += $Iss

Write-Host "`n--- compiling (LZMA2/ultra64 over ~670 MB; several minutes) ---" -ForegroundColor Cyan

# ISCC writes progress to stderr. Under Windows PowerShell 5.1 with
# $ErrorActionPreference = "Stop", a native command's stderr is promoted to a
# terminating NativeCommandError even on exit code 0 -- which would fail the
# build purely because of how this script was invoked (piped into Select-Object,
# say). Drop to Continue for the call and judge success by the exit code, which
# is the only reliable signal. Same fix as build_windows.ps1.
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    & $ISCC @isccArgs
    $code = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $prevEap
}
if ($code -ne 0) { throw "Inno Setup failed with exit code $code." }

# --- report -----------------------------------------------------------------
$Setup = Join-Path $OutDir "MatterClerk-Setup.exe"
if (-not (Test-Path $Setup)) { throw "Compile reported success but $Setup is missing." }

$setupBytes = (Get-Item $Setup).Length
$ratio = [math]::Round(($setupBytes / $bundleBytes) * 100, 1)

Write-Host "`n=== installer built ===" -ForegroundColor Green
Write-Host ("  {0}" -f $Setup)
Write-Host ("  {0} MB  (compressed from {1} MB -- {2}% of original)" -f `
    [math]::Round($setupBytes / 1MB, 1), [math]::Round($bundleBytes / 1MB, 1), $ratio)
Write-Host "`nInstalls to : $env:LOCALAPPDATA\Programs\MatterClerk"
Write-Host "Data stays in: $env:LOCALAPPDATA\MatterClerk"
Write-Host "`nSmartScreen will warn on first run -- the installer is unsigned, as designed."
