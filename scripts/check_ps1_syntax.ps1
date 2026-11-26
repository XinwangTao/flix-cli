<#
.SYNOPSIS
    Parse every .ps1 in the repo and report syntax errors.

.DESCRIPTION
    A PowerShell script with a syntax error only fails when someone runs it.
    None of them are covered by pytest, so this is the only thing watching them.

    Parsing does not execute anything, so it is safe on scripts that change the
    system (peer_apply_update.ps1, fix_firewall.ps1, ...).

    Also reports files that are not ASCII: PowerShell 5.1 reads a BOM-less .ps1
    as ANSI, so non-ASCII text in such a file silently corrupts. A UTF-8 BOM is
    fine (deliberate), non-ASCII without BOM is a hazard.
#>
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

$files = Get-ChildItem $repo -Filter *.ps1 -Recurse -File |
    Where-Object { $_.FullName -notmatch '\\(\.venv|node_modules|build|dist|dist_new|\.git)\\' }

$syntax = 0
$encoding = 0
foreach ($f in $files) {
    $rel = $f.FullName.Substring($repo.Length + 1)
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile(
        $f.FullName, [ref]$null, [ref]$errors)
    if ($errors -and $errors.Count -gt 0) {
        $syntax++
        $first = $errors[0]
        Write-Host ("FAIL syntax  {0}:{1}  {2}" -f $rel, $first.Extent.StartLineNumber, $first.Message) -ForegroundColor Red
    }

    $bytes = [IO.File]::ReadAllBytes($f.FullName)
    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    $nonAscii = 0
    foreach ($b in $bytes) { if ($b -gt 127) { $nonAscii++ } }
    if ($nonAscii -gt 0 -and -not $hasBom) {
        $encoding++
        Write-Host ("WARN encoding {0}: {1} non-ASCII bytes, no BOM (PS 5.1 will misread)" -f $rel, $nonAscii) -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host ("parsed {0} .ps1 files: {1} syntax error(s), {2} encoding hazard(s)" -f $files.Count, $syntax, $encoding)
if ($syntax -gt 0) { exit 1 }
exit 0
