<#
.SYNOPSIS
    Allow RelayDesk / flixd inbound traffic through Windows Firewall.

.DESCRIPTION
    Opens inbound TCP+UDP for one program so peers can reach the device
    channel. RelayDesk never runs this script: a blocked peer must be fixed
    by an administrator on that peer's own machine.

    Run in an elevated PowerShell:

        .\scripts\fix_firewall.ps1
        .\scripts\fix_firewall.ps1 -Program 'D:\apps\RelayDesk\flixd.exe'
        .\scripts\fix_firewall.ps1 -Remove

.PARAMETER Program
    Executable to allow. Defaults to dist\flixd.exe, then dist\RelayDesk.exe.

.PARAMETER Profile
    Firewall profiles to apply to. Default: Private only.

.PARAMETER Remove
    Delete the rules created by this script instead of adding them.

.NOTES
    Changes only this machine's firewall. Does not touch any remote host.
#>
[CmdletBinding()]
param(
    [string]$Program = "",
    [ValidateSet("Private", "Domain", "Public", "All")]
    [string]$Profile = "Private",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "ERROR: administrator rights required. Re-run from an elevated PowerShell." -ForegroundColor Red
    exit 5
}

$BaseName = "RelayDesk device channel"

if ($Remove) {
    $rules = Get-NetFirewallRule -DisplayName "$BaseName*" -ErrorAction SilentlyContinue
    if (-not $rules) {
        Write-Host "No '$BaseName' rules found; nothing to remove."
        exit 0
    }
    $rules | Remove-NetFirewallRule
    Write-Host ("Removed {0} rule(s): {1}" -f @($rules).Count, (($rules.DisplayName) -join ", "))
    exit 0
}

if (-not $Program) {
    $root = Split-Path -Parent $PSScriptRoot
    foreach ($candidate in @("dist\flixd.exe", "dist\RelayDesk.exe")) {
        $path = Join-Path $root $candidate
        if (Test-Path -LiteralPath $path) { $Program = $path; break }
    }
    if (-not $Program) {
        Write-Host "ERROR: no default executable found. Build dist first or pass -Program." -ForegroundColor Red
        exit 2
    }
}

if (-not (Test-Path -LiteralPath $Program)) {
    Write-Host "ERROR: executable not found: $Program" -ForegroundColor Red
    exit 2
}
$Program = (Resolve-Path -LiteralPath $Program).Path

$profileArg = switch ($Profile) {
    "All" { "Domain,Private,Public" }
    default { $Profile }
}

foreach ($proto in @("TCP", "UDP")) {
    $name = "$BaseName ($proto)"
    $existing = Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue
    if ($existing) {
        $existing | Set-NetFirewallRule -Enabled True -Action Allow -Profile $profileArg `
            -ErrorAction Stop
        Write-Host "Updated existing rule: $name -> $Program"
    } else {
        New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow `
            -Program $Program -Protocol $proto -Profile $profileArg `
            -Description "Allow RelayDesk peer traffic (protocol $proto)." | Out-Null
        Write-Host "Created rule: $name -> $Program"
    }
}

$active = Get-NetFirewallProfile -Profile $profileArg |
    ForEach-Object { "$($_.Name)=$($_.Enabled)" }
Write-Host ("Firewall profiles: {0}" -f ($active -join ", "))
Write-Host "Done. Verify from the peer with: flix netcheck"
