<#
.SYNOPSIS
    给一台机器装「开机自启 + 看门狗」：RelayDesk serve 掉线/重启后自动拉起。

.DESCRIPTION
    现场问题：那台机器反复卡死/重启后，没人把 serve 拉起来，设备通道和模型网关
    就一直不可用（表现为「RelayDesk 卡死」）。

    本脚本做两件事：
      1) 在「启动」目录写 RelayDeskServe.vbs —— 登录即后台起 serve；
      2) 建计划任务 RelayDeskWatchdog —— 每 5 分钟检查一次，没有 serve 就拉起。

    两处都只启动 serve（--device-port 18991 --api-port 18998），网关由 serve 按
    prefs 托管，不会重复监听。

.PARAMETER Exe
    RelayDesk.exe 的完整路径。

.PARAMETER Action
    Install（默认）/ Remove / Watch（计划任务调用，自检一次）/ Check（只报告）

.EXAMPLE
    .\peer_install_autostart.ps1 -Exe "C:\Users\Administrator\Downloads\RelayDesk-v2.6.5-win64\RelayDesk.exe" -Action Install
#>
[CmdletBinding()]
param(
    [string]$Exe = "",
    [ValidateSet("Install", "Remove", "Watch", "Check")]
    [string]$Action = "Install",
    [int]$DevicePort = 18991,
    [int]$ApiPort = 18998,
    [int]$GatewayPort = 18993,
    [string]$LogPath = "$env:TEMP\relaydesk-watchdog.log"
)

$ErrorActionPreference = "Continue"
$TaskName = "RelayDeskWatchdog"

function Log($m) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m
    try { Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8 } catch { }
    Write-Output $line
}

function Find-Exe {
    if ($Exe -and (Test-Path -LiteralPath $Exe)) { return (Resolve-Path -LiteralPath $Exe).Path }
    $candidates = @(
        (Join-Path $PSScriptRoot "..\dist\RelayDesk.exe"),
        (Join-Path $PSScriptRoot "RelayDesk.exe"),
        "$env:LOCALAPPDATA\Programs\RelayDesk\RelayDesk.exe"
    )
    $found = Get-ChildItem -Path "$env:USERPROFILE\Downloads" -Filter "RelayDesk*\RelayDesk.exe" `
        -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { $candidates = @($found.FullName) + $candidates }
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) { return (Resolve-Path -LiteralPath $c).Path }
    }
    return ""
}

function Get-ServeProcesses {
    @(Get-CimInstance Win32_Process -Filter "Name='RelayDesk.exe'" -ErrorAction SilentlyContinue |
      Where-Object { $_.CommandLine -match 'serve' })
}

function Start-Serve($path) {
    Start-Process -FilePath $path -WindowStyle Hidden `
        -ArgumentList @("serve", "--device-port", "$DevicePort", "--api-port", "$ApiPort")
}

switch ($Action) {
    "Check" {
        $procs = Get-ServeProcesses
        Write-Output ("serve processes: {0}" -f $procs.Count)
        $procs | Select-Object ProcessId, CommandLine | Format-Table -AutoSize
        try {
            $h = Invoke-WebRequest -Uri "http://127.0.0.1:$GatewayPort/health" -TimeoutSec 6 -UseBasicParsing
            Write-Output ("gateway: {0} {1}" -f $h.StatusCode, $h.Content)
        } catch { Write-Output ("gateway: DOWN ({0})" -f $_.Exception.Message) }
        exit 0
    }
    "Watch" {
        $path = Find-Exe
        if (-not $path) { Log "WATCH: 找不到 RelayDesk.exe"; exit 2 }

        function Probe-Gateway {
            try {
                $r = Invoke-WebRequest -Uri "http://127.0.0.1:$GatewayPort/health" `
                    -TimeoutSec 8 -UseBasicParsing
                return ($r.StatusCode -eq 200)
            } catch { return $false }
        }

        $strikeFile = Join-Path $env:TEMP "relaydesk-watchdog.strikes"
        $strikes = 0
        if (Test-Path -LiteralPath $strikeFile) {
            $strikes = [int]((Get-Content -LiteralPath $strikeFile -ErrorAction SilentlyContinue) -as [string])
        }

        $procs = Get-ServeProcesses
        $healthy = Probe-Gateway

        if ($healthy) {
            # 健康：清掉连续失败计数，安静退出（不写日志，避免每 5 分钟刷一条）
            if ($strikes -gt 0) { Remove-Item -LiteralPath $strikeFile -Force -ErrorAction SilentlyContinue }
            exit 0
        }

        if ($procs.Count -eq 0) {
            Log "WATCH: 没有 serve 进程，拉起 $path"
            Remove-Item -LiteralPath $strikeFile -Force -ErrorAction SilentlyContinue
        } else {
            # 进程活着但网关不应答 —— 就是「卡死」的样子。连续 2 次才动手，
            # 避免一次慢启动/瞬断就杀进程。
            $strikes++
            Set-Content -LiteralPath $strikeFile -Value "$strikes" -Encoding ASCII
            if ($strikes -lt 2) {
                Log ("WATCH: serve 进程在但网关无响应（连续 {0} 次），下次仍无响应则重启" -f $strikes)
                exit 0
            }
            Log ("WATCH: serve 进程在但网关连续 {0} 次无响应，强制重启" -f $strikes)
            foreach ($p in $procs) { & taskkill /PID $p.ProcessId /T /F *> $null }
            Start-Sleep -Seconds 4
            Remove-Item -LiteralPath $strikeFile -Force -ErrorAction SilentlyContinue
        }

        Start-Serve $path
        Start-Sleep -Seconds 12
        $after = Get-ServeProcesses
        Log ("WATCH: 拉起后 serve 进程 {0}" -f $after.Count)
        if (Probe-Gateway) {
            Log "WATCH: 网关 200，已恢复"
        } else {
            Log "WATCH: 网关仍不可用（可能整机卡死，需人工介入）"
        }
        exit 0
    }
    "Remove" {
        $startup = [Environment]::GetFolderPath("Startup")
        $vbs = Join-Path $startup "RelayDeskServe.vbs"
        if (Test-Path -LiteralPath $vbs) { Remove-Item -LiteralPath $vbs -Force; Log "已删除 $vbs" }
        & schtasks /delete /tn $TaskName /f *> $null
        Log "已删除计划任务 $TaskName"
        exit 0
    }
    "Install" {
        $path = Find-Exe
        if (-not $path) { Log "ERROR: 找不到 RelayDesk.exe，请用 -Exe 指定"; exit 2 }
        Log "使用 exe: $path"

        # 1) 登录自启（VBS，后台无窗口）
        $startup = [Environment]::GetFolderPath("Startup")
        $vbs = Join-Path $startup "RelayDeskServe.vbs"
        $lines = @(
            'Set sh = CreateObject("WScript.Shell")',
            ('sh.Run """{0}"" serve --device-port {1} --api-port {2}", 0, False' -f $path, $DevicePort, $ApiPort)
        )
        $lines | Out-File -FilePath $vbs -Encoding ASCII
        Log "已写入登录自启: $vbs"

        # 2) 看门狗计划任务（每 5 分钟）
        $self = (Resolve-Path -LiteralPath $PSCommandPath).Path
        $tr = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$self`" -Action Watch -Exe `"$path`""
        & schtasks /create /tn $TaskName /tr $tr /sc minute /mo 5 /f *> $null
        $ok = $LASTEXITCODE -eq 0
        Log ("计划任务 {0}: {1}" -f $TaskName, $(if ($ok) { "已创建（每 5 分钟）" } else { "创建失败 rc=$LASTEXITCODE" }))

        # 3) 立刻跑一次看门狗，确保当前就起着
        $procs = Get-ServeProcesses
        if ($procs.Count -eq 0) {
            Log "当前没有 serve，立即拉起"
            Start-Serve $path
            Start-Sleep -Seconds 10
        }
        try {
            $h = Invoke-WebRequest -Uri "http://127.0.0.1:$GatewayPort/health" -TimeoutSec 8 -UseBasicParsing
            Log ("网关自检 {0} {1}" -f $h.StatusCode, $h.Content.Substring(0, [Math]::Min(100, $h.Content.Length)))
        } catch { Log ("网关自检失败: {0}" -f $_.Exception.Message) }
        exit 0
    }
}
