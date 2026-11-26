<#
    Read-only: prove that this machine (the cascade consumer) has NO VPN path,
    yet still gets models through the LAN relay.

    That is the whole point of the cascade feature, so it deserves a check from
    the peer's own point of view.

    PURE ASCII ON PURPOSE -- PowerShell 5.1 reads a BOM-less .ps1 as ANSI, so
    Chinese text here would turn into mojibake and break parsing. Keep it ASCII.
#>
$out = Join-Path $env:TEMP "rd-vpn-check.txt"
"=== vpn check $(Get-Date -Format o) ===" | Out-File -FilePath $out -Encoding UTF8
function Say($t) { $t | Out-File -FilePath $out -Append -Encoding UTF8 }

Say "--- Sangfor / EasyConnect processes ---"
$vpn = @(Get-Process -ErrorAction SilentlyContinue |
         Where-Object { $_.ProcessName -match 'Sangfor|EasyConnect' })
if ($vpn.Count -eq 0) {
    Say "  none (0)"
} else {
    $vpn | ForEach-Object { Say ("  " + $_.ProcessName + " pid=" + $_.Id) }
}

Say "--- Sangfor services ---"
$svc = @(Get-Service -ErrorAction SilentlyContinue |
         Where-Object { $_.Name -match 'Sangfor' })
if ($svc.Count -eq 0) { Say "  none" }
$svc | ForEach-Object { Say ("  " + $_.Name + " = " + $_.Status) }

Say "--- TCP 443 to claude.jlcops.com (reachable only with VPN) ---"
$tcp = New-Object System.Net.Sockets.TcpClient
try {
    $task = $tcp.ConnectAsync("claude.jlcops.com", 443)
    $ok = $task.Wait(6000)
    if ($ok -and $tcp.Connected) { Say "  reachable (direct upstream works)" }
    else { Say "  unreachable (expected: no VPN)" }
} catch {
    Say ("  unreachable: " + $_.Exception.GetType().Name)
} finally {
    $tcp.Close()
}

Say "--- gateway upstream config ---"
$prefsPath = Join-Path $env:APPDATA "flixcli\shared_preferences.json"
if (Test-Path $prefsPath) {
    $p = Get-Content -LiteralPath $prefsPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($p.modelGateway) {
        Say ("  upstream = " + $p.modelGateway.upstream)
        Say ("  allow_remote_upstream = " + $p.modelGateway.allow_remote_upstream)
        Say ("  upstream_token set = " + [bool]$p.modelGateway.upstream_token)
    } else { Say "  no modelGateway config" }
} else { Say "  prefs not found" }

Say "--- model count from the local gateway ---"
try {
    $h = Invoke-WebRequest -Uri "http://127.0.0.1:18993/v1/models" `
         -Headers @{ "x-api-key" = $p.modelGateway.token } -TimeoutSec 10 -UseBasicParsing
    $d = $h.Content | ConvertFrom-Json
    Say ("  " + @($d.data).Count + " models")
} catch { Say ("  failed: " + $_.Exception.Message) }

Say "=== done ==="
Write-Output "written: $out"
