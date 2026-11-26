<#
.SYNOPSIS
    从任意一台局域网机器验证「本机通过 LAN 使用另一台机器的模型网关」。

.DESCRIPTION
    给 .200 这类「自己不登 VPN」的机器用：它不直连 claude.jlcops.com，
    而是把请求打到有 VPN 的那台机器（默认 192.168.237.7）的 RelayDesk 网关，
    由对方的 cc-switch 转发到模型。本脚本只做只读验证，不改任何配置。

    -Health  : 网关是否在线（无令牌，任何人可查）
    -Models  : 带令牌列出模型（Anthropic/OpenAI 双字段）
    -Chat    : 真正发一次补全，证明「局域网→网关→cc-switch→模型」整链通

.EXAMPLE
    .\check_lan_model_source.ps1 -Token rd-xxxx
    .\check_lan_model_source.ps1 -Host 192.168.237.7 -Port 18993 -Token rd-xxxx -Chat
#>
[CmdletBinding()]
param(
    [string]$ModelSourceHost = "192.168.237.7",
    [int]$Port = 18993,
    # 令牌可以走环境变量，方便在别的机器上一行跑完（也免去写在命令行历史里）。
    [string]$Token = $env:RD_GATEWAY_TOKEN,
    [string]$Model = "glm-5.3-flash",
    [switch]$Models,
    [switch]$Chat,
    [switch]$All,
    [int]$TimeoutSec = 30
)

$ErrorActionPreference = "Stop"
$base = "http://${ModelSourceHost}:${Port}"
$ok = $true

function Step($name, [scriptblock]$body) {
    try {
        $detail = & $body
        Write-Host ("PASS  {0}  {1}" -f $name, $detail) -ForegroundColor Green
    } catch {
        $script:ok = $false
        Write-Host ("FAIL  {0}  {1}" -f $name, $_.Exception.Message) -ForegroundColor Red
    }
}

Write-Host ("模型源: {0}   模型: {1}" -f $base, $Model) -ForegroundColor Cyan

Step "tcp-connect" {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync($ModelSourceHost, $Port)
        if (-not $task.Wait([TimeSpan]::FromSeconds(5))) {
            throw "连接超时（防火墙未放行？在对端用 scripts\fix_firewall.ps1 -Profile Private）"
        }
        "connected"
    } finally { $client.Dispose() }
}

Step "health" {
    $r = Invoke-WebRequest -Uri "$base/health" -TimeoutSec $TimeoutSec -UseBasicParsing
    "status=$($r.StatusCode) body=$($r.Content.Substring(0,[Math]::Min(80,$r.Content.Length)))"
}

if ($Models -or $All -or -not $Chat) {
    Step "models" {
        if (-not $Token) { throw "需要 -Token（对端 flix models status 可取）" }
        $r = Invoke-WebRequest -Uri "$base/v1/models" -Headers @{ "x-api-key" = $Token } `
            -TimeoutSec $TimeoutSec -UseBasicParsing
        $j = $r.Content | ConvertFrom-Json
        $ids = @($j.data | ForEach-Object { $_.id })
        if ($ids.Count -eq 0) { throw "模型列表为空" }
        "共 {0} 个，前3: {1}" -f $ids.Count, ($ids[0..([Math]::Min(2, $ids.Count - 1))] -join ", ")
    }
}

if ($Chat -or $All) {
    Step "chat (Anthropic 协议)" {
        if (-not $Token) { throw "需要 -Token" }
        $body = @{
            model       = $Model
            max_tokens  = 300
            messages    = @(@{ role = "user"; content = "只回答两个字：通了" })
        } | ConvertTo-Json -Depth 6
        $r = Invoke-WebRequest -Uri "$base/v1/messages" -Method POST `
            -Headers @{ "x-api-key" = $Token; "anthropic-version" = "2023-06-01" } `
            -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 120 -UseBasicParsing
        $j = $r.Content | ConvertFrom-Json
        if ($j.type -eq "error") { throw $j.error.message }
        $text = ($j.content | Where-Object { $_.type -eq "text" } |
                 ForEach-Object { $_.text }) -join ""
        "route=$($r.Headers['x-relaydesk-route']) model=$($j.model) text='$($text.Trim())'"
    }

    Step "chat (OpenAI 协议)" {
        if (-not $Token) { throw "需要 -Token" }
        $body = @{
            model      = $Model
            max_tokens = 300
            messages   = @(@{ role = "user"; content = "只回答两个字：通了" })
        } | ConvertTo-Json -Depth 6
        $r = Invoke-WebRequest -Uri "$base/v1/chat/completions" -Method POST `
            -Headers @{ "authorization" = "Bearer $Token" } `
            -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 120 -UseBasicParsing
        $j = $r.Content | ConvertFrom-Json
        $text = $j.choices[0].message.content
        "model=$($j.model) text='$(([string]$text).Trim())'"
    }
}

Write-Host ""
if ($ok) { Write-Host "全部通过：这台机器不需要登录 VPN 也能用模型。" -ForegroundColor Green; exit 0 }
Write-Host "有失败项，见上面 FAIL 行。" -ForegroundColor Red; exit 1
