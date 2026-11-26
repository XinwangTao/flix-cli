<# 只读：查对端 RelayDesk 进程、端口监听、防火墙状态。 #>
$out = Join-Path $env:TEMP "rd-ports.txt"
"=== ports $(Get-Date -Format o) ===" | Out-File -FilePath $out -Encoding UTF8
function Say($t) { $t | Out-File -FilePath $out -Append -Encoding UTF8 }

Say "--- RelayDesk 进程 ---"
Get-CimInstance Win32_Process -Filter "Name='RelayDesk.exe'" -ErrorAction SilentlyContinue |
    ForEach-Object {
        Say ("pid={0} ppid={1} :: {2}" -f $_.ProcessId, $_.ParentProcessId, $_.CommandLine)
    }

Say "--- 监听端口 ---"
foreach ($port in 8891, 18991, 18993, 18998, 15721) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        Say ("{0}: pid={1}" -f $port, ($conn | Select-Object -First 1).OwningProcess)
    } else {
        Say ("{0}: 无人监听" -f $port)
    }
}

Say "--- 防火墙 ==="
try {
    Get-NetFirewallProfile | ForEach-Object {
        Say ("profile {0} enabled={1}" -f $_.Name, $_.Enabled)
    }
} catch { Say "firewall query failed: $($_.Exception.Message)" }

Say "--- 18991 相关规则 ---"
try {
    Get-NetFirewallPortFilter -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalPort -match '18991|18993' } |
        Select-Object -First 8 |
        ForEach-Object { Say ("rule port={0} proto={1}" -f $_.LocalPort, $_.Protocol) }
} catch { Say "rules query failed" }

Say "--- 看门狗日志尾部 ---"
$log = Join-Path $env:TEMP "relaydesk-watchdog.log"
if (Test-Path $log) { Get-Content $log -Tail 8 | ForEach-Object { Say $_ } } else { Say "(无日志)" }

Say "--- 计划任务 ---"
try {
    Get-ScheduledTask -TaskName RelayDeskWatchdog -ErrorAction Stop |
        ForEach-Object { Say ("task {0} state={1}" -f $_.TaskName, $_.State) }
    Get-ScheduledTaskInfo -TaskName RelayDeskWatchdog -ErrorAction SilentlyContinue |
        ForEach-Object { Say ("last={0} result={1}" -f $_.LastRunTime, $_.LastTaskResult) }
} catch { Say "task query failed" }

Say "=== done ==="
Write-Output "written: $out"
