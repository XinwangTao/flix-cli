<#
    只读：确认 EDR 产品身份 + Windows 遥测任务状态 + 三个 guard 脚本的计划任务详情。
#>
$out = Join-Path $env:TEMP "rd-edr.txt"
"=== edr $(Get-Date -Format o) ===" | Out-File -FilePath $out -Encoding UTF8
function Say($t) { $t | Out-File -FilePath $out -Append -Encoding UTF8 }

Say "--- ZtsmEnt 产品信息 ---"
$dir = 'C:\Program Files (x86)\ZtsmEnt'
if (Test-Path $dir) {
    Get-ChildItem $dir -ErrorAction SilentlyContinue |
        Select-Object -First 6 | ForEach-Object { Say ("  " + $_.Name) }
    Say "--- 服务 ---"
    Get-Service -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match 'ztsm|Ztsm' } |
        ForEach-Object { Say ("  svc {0} = {1} ({2})" -f $_.Name, $_.Status, $_.DisplayName) }
    Say "--- 版本文件 ---"
    Get-ChildItem $dir -Recurse -Depth 2 -Filter '*.exe' -ErrorAction SilentlyContinue |
        Select-Object -First 6 | ForEach-Object {
            Say ("  {0}  {1}" -f $_.Name, $_.VersionInfo.ProductName)
        }
}

Say "--- Windows 兼容性遥测任务 ---"
try {
    Get-ScheduledTask -TaskPath '\Microsoft\Windows\Application Experience\*' -ErrorAction Stop |
        ForEach-Object {
            Say ("  {0} state={1}" -f $_.TaskName, $_.State)
        }
} catch { Say "  query failed: $($_.Exception.Message)" }

Say "--- 三个 guard 任务详情 ---"
foreach ($name in @('PCCrashGuard', 'SMT-ResourceGuard')) {
    try {
        $t = Get-ScheduledTask -TaskName $name -ErrorAction Stop
        Say ("  {0} state={1}" -f $t.TaskName, $t.State)
        foreach ($tr in $t.Triggers) {
            Say ("    trigger: {0}" -f $tr.CimClass.CimClassName)
        }
        $info = Get-ScheduledTaskInfo -TaskName $t.TaskName -ErrorAction SilentlyContinue
        if ($info) {
            Say ("    last={0} result={1}" -f $info.LastRunTime, $info.LastTaskResult)
        }
    } catch { Say ("  {0}: {1}" -f $name, $_.Exception.Message) }
}

Say "--- 这两个 guard 的日志尾部（若有） ---"
foreach ($log in @('D:\_diag\resource_guard.log',
                   'C:\dsh-fix\resource-guard-v3.log',
                   'C:\Users\Administrator\AppData\Local\PCCrashGuard\guard.log')) {
    if (Test-Path $log) {
        Say ("### {0}  ({1} 字节)" -f $log, (Get-Item $log).Length)
        Get-Content $log -Tail 6 -ErrorAction SilentlyContinue |
            ForEach-Object { Say ("  | " + $_) }
    }
}

Say "=== done ==="
Write-Output "written: $out"
