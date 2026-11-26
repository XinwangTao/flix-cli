<#
    Runs ON the peer: replace RelayDesk.exe with a new build and configure this
    machine as a cascade relay (upstream = the VPN machine).

    MUST be launched by schtasks, not as an RDShell child: this script kills
    RelayDesk, and the RDShell executor lives inside it.

    PURE ASCII ON PURPOSE. PowerShell 5.1 reads a BOM-less .ps1 as ANSI, so any
    Chinese text in here turns into mojibake and the whole script fails to parse
    ("unexpected token"). Keeping every literal ASCII removes that failure mode
    entirely -- that has bitten this file more than once.
#>
param(
    [string]$NewExe = "C:\Users\Administrator\Downloads\RelayDesk (2).exe",
    [string]$TargetDir = "C:\Users\Administrator\Downloads\RelayDesk-v2.6.5-win64",
    [string]$ModelSource = "http://192.168.237.7:18993",
    [string]$ModelSourceToken = "",
    [string]$LogPath = "C:\Users\Administrator\Downloads\rd-update.log"
)

$ErrorActionPreference = "Continue"
function Log($m) {
    $line = "{0}  {1}" -f (Get-Date -Format "HH:mm:ss"), $m
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
}

Log "=== start ==="
Log "new exe : $NewExe"
Log "target  : $TargetDir"

Start-Sleep -Seconds 3

try {
    if (-not (Test-Path -LiteralPath $NewExe)) { Log "ERROR: new exe missing"; exit 2 }
    $newInfo = Get-Item -LiteralPath $NewExe
    Log ("new exe size {0}" -f $newInfo.Length)

    # 1) stop every RelayDesk (GUI and serve)
    $procs = @(Get-Process RelayDesk -ErrorAction SilentlyContinue)
    Log ("stopping {0} process(es): {1}" -f $procs.Count, ($procs.Id -join ","))
    foreach ($p in $procs) {
        try { Stop-Process -Id $p.Id -Force -ErrorAction Stop }
        catch { Log ("  stop {0} failed: {1}" -f $p.Id, $_.Exception.Message) }
    }
    Start-Sleep -Seconds 3

    $left = @(Get-Process RelayDesk -ErrorAction SilentlyContinue)
    Log ("leftover processes {0}" -f $left.Count)
    foreach ($p in $left) { & taskkill /PID $p.Id /T /F *> $null }
    Start-Sleep -Seconds 2

    # 2) back up and replace
    $target = Join-Path $TargetDir "RelayDesk.exe"
    if (Test-Path -LiteralPath $target) {
        $bak = Join-Path $TargetDir ("RelayDesk.exe.prev-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
        Move-Item -LiteralPath $target -Destination $bak -Force
        Log ("backup -> {0}" -f (Split-Path $bak -Leaf))
    }
    Copy-Item -LiteralPath $NewExe -Destination $target -Force
    $after = (Get-Item -LiteralPath $target).Length
    Log ("replaced, size {0}" -f $after)
    if ($after -ne $newInfo.Length) { Log "WARNING: size mismatch" }

    # 3) cascade: point upstream at the machine that has the VPN
    $prefsPath = Join-Path $env:APPDATA "flixcli\shared_preferences.json"
    if (Test-Path -LiteralPath $prefsPath) {
        $prefs = Get-Content -LiteralPath $prefsPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not $prefs.modelGateway) {
            $prefs | Add-Member -NotePropertyName modelGateway -NotePropertyValue ([pscustomobject]@{}) -Force
        }
        $gw = $prefs.modelGateway
        $set = @{
            enabled = $true
            port = 18993
            upstream = $ModelSource
            allow_remote_upstream = $true
        }
        if ($ModelSourceToken) { $set.upstream_token = $ModelSourceToken }
        foreach ($k in $set.Keys) {
            if ($gw.PSObject.Properties.Name -contains $k) { $gw.$k = $set[$k] }
            else { $gw | Add-Member -NotePropertyName $k -NotePropertyValue $set[$k] -Force }
        }
        $json = $prefs | ConvertTo-Json -Depth 8
        [IO.File]::WriteAllText($prefsPath, $json, (New-Object Text.UTF8Encoding($false)))
        $tokState = "unset"
        if ($ModelSourceToken) { $tokState = "set" }
        Log ("cascade configured: upstream={0} token={1}" -f $ModelSource, $tokState)
    } else {
        Log "WARN: prefs not found, skipped cascade config"
    }

    # 4) start serve (device 18991 + Desktop API 18998 + gateway 18993)
    Start-Process -FilePath $target -WindowStyle Hidden `
        -ArgumentList @("serve", "--device-port", "18991", "--api-port", "18998")
    Start-Sleep -Seconds 10

    $running = @(Get-Process RelayDesk -ErrorAction SilentlyContinue)
    Log ("processes after start {0}: {1}" -f $running.Count, ($running.Id -join ","))

    # 5) self check
    try {
        $h = Invoke-WebRequest -Uri "http://127.0.0.1:18993/health" -TimeoutSec 8 -UseBasicParsing
        Log ("gateway healthy {0} {1}" -f $h.StatusCode, $h.Content.Substring(0, [Math]::Min(120, $h.Content.Length)))
    } catch {
        Log ("gateway probe failed: {0}" -f $_.Exception.Message)
    }

    Log "=== done ==="
} catch {
    Log ("ERROR: {0}" -f $_.Exception.Message)
    exit 1
}
