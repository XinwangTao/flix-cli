<#
.SYNOPSIS
    Reclaim disk from stale PyInstaller _MEI* temp directories.

.DESCRIPTION
    A PyInstaller one-file exe unpacks itself into %TEMP%\_MEIxxxxxx while it runs
    and removes that directory on a clean exit. Killing the process (taskkill /F,
    which the watchdog and the peer update script both do) leaves the directory
    behind -- about 81 MB per kill. This repo's own machine accumulated 664 of
    them / 9.2 GB in two days.

    This only deletes directories that are BOTH:
      - older than -Hours (default 24), and
      - not owned by any running RelayDesk process.
    It never touches anything outside %TEMP%\ _MEI*, and it prints what it would
    do before doing it unless -Quiet.

    ASCII only (PowerShell 5.1 reads a BOM-less .ps1 as ANSI).

.PARAMETER Hours
    Only consider directories whose LastWriteTime is older than this. Default 24.

.PARAMETER DryRun
    Only report; delete nothing.

.EXAMPLE
    .\scripts\clean_mei.ps1
    .\scripts\clean_mei.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [int]$Hours = 24,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$temp = $env:TEMP
if (-not $temp -or -not (Test-Path $temp)) { Write-Host "no %TEMP%"; exit 1 }

Write-Host "temp: $temp"
Write-Host ("considering _MEI* older than {0}h ({1})" -f $Hours, (Get-Date).AddHours(-$Hours).ToString("yyyy-MM-dd HH:mm"))

# Directories currently used by a running RelayDesk: never delete these, however
# old they look (a long-running serve keeps its unpack dir open the whole time).
$protected = @{}
foreach ($p in (Get-Process RelayDesk -ErrorAction SilentlyContinue)) {
    foreach ($d in (Get-ChildItem $temp -Directory -Filter "_MEI*" -ErrorAction SilentlyContinue)) {
        if ($d.LastWriteTime -ge $p.StartTime.AddMinutes(-3) -and
            $d.LastWriteTime -le $p.StartTime.AddMinutes(3)) {
            $protected[$d.Name] = $p.Id
        }
    }
}
if ($protected.Count -gt 0) {
    foreach ($name in $protected.Keys) {
        Write-Host ("  keep (in use by pid " + $protected[$name] + "): " + $name) -ForegroundColor Cyan
    }
} else {
    Write-Host "  no running RelayDesk process matched a _MEI dir" -ForegroundColor DarkGray
}

$cutoff = (Get-Date).AddHours(-$Hours)
$total = 0
$freed = 0
$kept = 0
foreach ($d in (Get-ChildItem $temp -Directory -Filter "_MEI*" -ErrorAction SilentlyContinue)) {
    $total++
    if ($protected.ContainsKey($d.Name)) { $kept++; continue }
    if ($d.LastWriteTime -ge $cutoff) { $kept++; continue }
    $size = (Get-ChildItem $d.FullName -Recurse -File -ErrorAction SilentlyContinue |
             Measure-Object -Property Length -Sum).Sum
    if ($DryRun) {
        Write-Host ("  would delete {0}  {1} MB  {2}" -f $d.Name, [math]::Round($size/1MB,0), $d.LastWriteTime.ToString("MM-dd HH:mm"))
        $freed += $size
        continue
    }
    try {
        Remove-Item $d.FullName -Recurse -Force -ErrorAction Stop
        $freed += $size
    } catch {
        Write-Host ("  could not delete {0}: {1}" -f $d.Name, $_.Exception.Message) -ForegroundColor Yellow
        $kept++
    }
}

$verb = if ($DryRun) { "would free" } else { "freed" }
Write-Host ""
Write-Host ("{0} _MEI dirs: {1} {2:N0} MB, {3} kept" -f $total, $verb, ($freed/1MB), $kept)
if (-not $DryRun -and $freed -gt 0) {
    $drive = (Get-Item $temp).PSDrive
    Write-Host ("{0}: now {1:N1} GB free" -f $drive.Name, ($drive.Free/1GB))
}
exit 0
