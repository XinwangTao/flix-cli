<#
.SYNOPSIS
    在不登录 VPN 的机器上，一键把模型源指向局域网里有 VPN 的那台机器（级联中继）。

.DESCRIPTION
    场景：.200 自己不登 EasyConnect，但要用模型。
    做法：在本机跑一个 RelayDesk 网关，上游指向对端网关（默认 192.168.237.7:18993），
    由对端用它的 VPN 转发。客户端只需要连本机的 :18993，无需关心 VPN。

    本脚本会：
      1) 检查到模型源的连通性（TCP + /health）；
      2) 把模型源写成 prefs 里的 modelGateway（enabled / upstream / upstream_token /
         allow_remote_upstream），并启动 serve；
      3) 用本机令牌跑一次真实补全，验证 客户端→本机网关→对端网关→模型 整链；
      4) 可选 -InstallAutostart 注册登录自启（VBS），避免重启后又"卡死"没了服务。

.PARAMETER ModelSource
    有 VPN 的那台机器的网关地址，例如 http://192.168.237.7:18993

.PARAMETER ModelSourceToken
    对端网关的令牌（在对端执行 `flix models status` 查看 "令牌:" 行）

.EXAMPLE
    .\setup_lan_model_source.ps1 -ModelSource http://192.168.237.7:18993 `
        -ModelSourceToken rd-xxxx -InstallAutostart
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ModelSource,
    [Parameter(Mandatory = $true)][string]$ModelSourceToken,
    [int]$Port = 18993,
    [int]$DevicePort = 18991,
    [int]$ApiPort = 18998,
    [string]$Model = "glm-5.3-flash",
    [string]$RelayDesk = "",
    [switch]$InstallAutostart,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
function Say($m, $c = "Gray") { Write-Host $m -ForegroundColor $c }

# --- 定位 RelayDesk.exe -----------------------------------------------------
if (-not $RelayDesk) {
    $candidates = @(
        (Join-Path $PSScriptRoot "..\dist\RelayDesk.exe"),
        (Join-Path $PSScriptRoot "..\..\dist\RelayDesk.exe"),
        "$env:LOCALAPPDATA\Programs\RelayDesk\RelayDesk.exe",
        (Join-Path $PSScriptRoot "RelayDesk.exe")
    )
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) { $RelayDesk = (Resolve-Path -LiteralPath $c).Path; break }
    }
}
if (-not $RelayDesk -or -not (Test-Path -LiteralPath $RelayDesk)) {
    Say "ERROR: 找不到 RelayDesk.exe，请用 -RelayDesk 指定完整路径" Red
    exit 2
}
Say "RelayDesk: $RelayDesk"

# --- 1. 模型源连通性 --------------------------------------------------------
Say "检查模型源 $ModelSource ..."
try {
    $h = Invoke-WebRequest -Uri "$ModelSource/health" -TimeoutSec 8 -UseBasicParsing
    Say ("  健康检查 {0}  {1}" -f $h.StatusCode, $h.Content.Substring(0, [Math]::Min(80, $h.Content.Length))) Green
} catch {
    Say "  模型源不可达: $($_.Exception.Message)" Red
    Say "  先确认那台机器开着、网关在监听、且双方在同一网段。" Red
    exit 3
}
try {
    $m = Invoke-WebRequest -Uri "$ModelSource/v1/models" -Headers @{ "x-api-key" = $ModelSourceToken } `
        -TimeoutSec 15 -UseBasicParsing
    $count = @((($m.Content | ConvertFrom-Json).data)).Count
    Say ("  模型源令牌可用，共 {0} 个模型" -f $count) Green
    if ($count -eq 0) { Say "  警告：模型源返回 0 个模型" Yellow }
} catch {
    Say "  模型源令牌校验失败: $($_.Exception.Message)" Red
    Say "  令牌请在对端执行：flix models status（看 '令牌:' 行）" Red
    exit 4
}

# --- 2. 写配置（写 prefs，由 serve 负责拉起网关） ---------------------------
# 注意：不要用 `models start` 来写配置——它是前台常驻命令，会把脚本卡住。
# 网关配置真源是 %APPDATA%\flixcli\shared_preferences.json 的 modelGateway 节点。
$prefsPath = Join-Path $env:APPDATA "flixcli\shared_preferences.json"
if (-not (Test-Path -LiteralPath $prefsPath)) {
    Say "ERROR: 找不到 $prefsPath（先运行一次 RelayDesk）" Red
    exit 5
}
if ($DryRun) {
    Say "[DryRun] 将写入 enabled=true port=$Port upstream=$ModelSource upstream_token=*** allow_remote_upstream=true" Yellow
    Say "[DryRun] 将重启 serve（--device-port $DevicePort --api-port $ApiPort）并做整链验证" Yellow
    Say "[DryRun] 结束，未修改任何状态" Green
    exit 0
}
$prefs = Get-Content -LiteralPath $prefsPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $prefs.modelGateway) {
    $prefs | Add-Member -NotePropertyName modelGateway -NotePropertyValue ([pscustomobject]@{}) -Force
}
$gw = $prefs.modelGateway
foreach ($pair in @(
        @("enabled", $true),
        @("port", $Port),
        @("upstream", $ModelSource.TrimEnd("/")),
        @("upstream_token", $ModelSourceToken),
        @("allow_remote_upstream", $true))) {
    $name = $pair[0]; $value = $pair[1]
    if ($gw.PSObject.Properties.Name -contains $name) { $gw.$name = $value }
    else { $gw | Add-Member -NotePropertyName $name -NotePropertyValue $value -Force }
}
$json = $prefs | ConvertTo-Json -Depth 8
[IO.File]::WriteAllText($prefsPath, $json, (New-Object Text.UTF8Encoding($false)))
Say ("已写入配置: upstream={0} upstream_token={1}…" -f $gw.upstream, $ModelSourceToken.Substring(0, [Math]::Min(6, $ModelSourceToken.Length))) Green

# --- 3. 重启 serve 让它按新配置托管网关 ------------------------------------
$running = @(Get-CimInstance Win32_Process -Filter "Name='RelayDesk.exe'" |
             Where-Object { $_.CommandLine -match 'serve' })
foreach ($p in $running) {
    Say "停止现有 serve (PID $($p.ProcessId))"
    & taskkill /PID $p.ProcessId /T /F *> $null
}
Start-Sleep -Seconds 3
Say "启动 serve ..."
Start-Process -FilePath $RelayDesk -WindowStyle Hidden `
    -ArgumentList @("serve", "--device-port", "$DevicePort", "--api-port", "$ApiPort")
Start-Sleep -Seconds 10

# --- 4. 读本机网关令牌（serve 启动时若没有会铸一个并写回 prefs） ------------
$myToken = ""
for ($i = 0; $i -lt 20; $i++) {
    $fresh = Get-Content -LiteralPath $prefsPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($fresh.modelGateway -and $fresh.modelGateway.token) {
        $myToken = [string]$fresh.modelGateway.token
        break
    }
    Start-Sleep -Seconds 1
}
if (-not $myToken) {
    Say "  无法读取本机网关令牌（用 `flix models status` 手动确认）" Yellow
}

# --- 5. 整链验证（本机网关 -> 对端网关 -> 模型） ----------------------------
Say "整链验证（本机网关 -> 对端网关 -> 模型）..."
if ($myToken) {
    $body = @{
        model      = $Model
        max_tokens = 300
        messages   = @(@{ role = "user"; content = "只回答两个字：通了" })
    } | ConvertTo-Json -Depth 6
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/v1/messages" -Method POST `
            -Headers @{ "x-api-key" = $myToken; "anthropic-version" = "2023-06-01" } `
            -ContentType "application/json; charset=utf-8" `
            -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 180 -UseBasicParsing
        $j = $r.Content | ConvertFrom-Json
        $text = ($j.content | Where-Object { $_.type -eq "text" } | ForEach-Object { $_.text }) -join ""
        Say ("  OK route={0} model={1} 回答='{2}'" -f $r.Headers['x-relaydesk-route'], $j.model, $text.Trim()) Green
    } catch {
        Say "  整链验证失败: $($_.Exception.Message)" Red
        Say "  排查：对端网关是否能把请求送到模型（在对端跑 scripts\check_lan_model_source.ps1）" Yellow
    }
}

# --- 4. 自启 ----------------------------------------------------------------
if ($InstallAutostart) {
    $startup = [Environment]::GetFolderPath("Startup")
    $vbs = Join-Path $startup "RelayDeskServe.vbs"
    $cmd = "/c start """" /min ""{0}"" serve --device-port {1} --api-port {2}" -f $RelayDesk, $DevicePort, $ApiPort
    $lines = @(
        'Set sh = CreateObject("WScript.Shell")',
        ('sh.Run "{0}", 0, False' -f $cmd)
    )
    $lines | Out-File -FilePath $vbs -Encoding ASCII
    Say "已注册登录自启: $vbs" Green
}

Say ""
Say "完成。客户端指向 http://127.0.0.1:$Port 即可（令牌见 flix models status）。" Green
Say "本机不需要登录 VPN。" Green
