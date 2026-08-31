#Requires -Version 5.1
<#
.SYNOPSIS
    Download the bge-small-en-v1.5 ONNX weights and tokenizer into models/.

.DESCRIPTION
    models/ is gitignored (~133 MB). This is the only step in the build that
    needs internet; the resulting bundle is fully offline.
#>
[CmdletBinding()]
param(
    [string]$Revision = "main"
)

$ErrorActionPreference = "Stop"
$Root  = Split-Path -Parent $PSScriptRoot
$Dest  = Join-Path $Root "models\bge-small-en-v1.5"
$Base  = "https://huggingface.co/BAAI/bge-small-en-v1.5/resolve/$Revision"

New-Item -ItemType Directory -Path (Join-Path $Dest "onnx") -Force | Out-Null

# TLS 1.2 is not the default in Windows PowerShell 5.1 and huggingface.co
# refuses anything older.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$files = @(
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "vocab.txt",
    "onnx/model.onnx"
)

foreach ($f in $files) {
    $out = Join-Path $Dest ($f -replace '/', '\')
    Write-Host "  $f"
    Invoke-WebRequest -Uri "$Base/$f" -OutFile $out -UseBasicParsing
}

$onnx = Join-Path $Dest "onnx\model.onnx"
$size = (Get-Item $onnx).Length
if ($size -lt 100MB) {
    throw "onnx/model.onnx is only $size bytes -- download looks truncated."
}
Write-Host "Model staged: $([math]::Round($size / 1MB, 1)) MB" -ForegroundColor Green
