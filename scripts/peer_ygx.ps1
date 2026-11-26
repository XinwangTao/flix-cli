<#
    只读：追查 D:\ygx_consumer\consumer.py 这个内存失控脚本——谁起的、跑什么、有没有日志。
#>
$out = Join-Path $env:TEMP "rd-ygx.txt"
"=== ygx $(Get-Date -Format o) ===" | Out-File -FilePath $out -Encoding UTF8
function Say($t) { $t | Out-File -FilePath $out -Append -Encoding UTF8 }

Say "--- 当前 ygx_consumer 进程 ---"
$all = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
$byId = @{}
foreach ($p in $all) { $byId[[int]$p.ProcessId] = $p }
$targets = $all | Where-Object { $_.CommandLine -match 'ygx_consumer' }
if (-not $targets) { Say "  (当前没有在跑)" }
foreach ($t in $targets) {
    $live = Get-Process -Id $t.ProcessId -ErrorAction SilentlyContinue
    $mb = if ($live) { [math]::Round($live.WorkingSet64 / 1MB, 0) } else { -1 }
    Say ("  pid={0} ppid={1} mem={2}MB started={3}" -f $t.ProcessId, $t.ParentProcessId, $mb, $t.CreationDate)
    $parent = $byId[[int]$t.ParentProcessId]
    if ($parent) {
        Say ("    父: {0} pid={1} :: {2}" -f $parent.Name, $parent.ProcessId, [string]$parent.CommandLine)
        $grand = $byId[[int]$parent.ParentProcessId]
        if ($grand) { Say ("    祖父: {0} pid={1} :: {2}" -f $grand.Name, $grand.ProcessId, [string]$grand.CommandLine) }
    }
}

Say "--- 目录内容 ---"
$dir = 'D:\ygx_consumer'
if (Test-Path $dir) {
    Get-ChildItem $dir -ErrorAction SilentlyContinue | Select-Object -First 15 |
        ForEach-Object { Say ("  {0,-40} {1,10} 字节  {2}" -f $_.Name, $_.Length, $_.LastWriteTime) }
    Say "--- consumer.py 头部 30 行 ---"
    $py = Join-Path $dir 'consumer.py'
    if (Test-Path $py) {
        Get-Content $py -TotalCount 30 -ErrorAction SilentlyContinue |
            ForEach-Object { Say ("  | " + $_) }
    }
    Say "--- 日志尾部（若有） ---"
    Get-ChildItem $dir -Filter '*.log' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 2 |
        ForEach-Object {
            Say ("  ### {0}" -f $_.Name)
            Get-Content $_.FullName -Tail 6 -ErrorAction SilentlyContinue |
                ForEach-Object { Say ("  | " + $_) }
        }
} else {
    Say "  目录不存在"
}

Say "--- 相关计划任务 ---"
try {
    Get-ScheduledTask -ErrorAction Stop | ForEach-Object {
        $act = ($_.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join ' | '
        if ($act -match 'ygx') {
            $info = Get-ScheduledTaskInfo -TaskName $_.TaskName -TaskPath $_.TaskPath -ErrorAction SilentlyContinue
            Say ("  {0} state={1} last={2} result={3}" -f $_.TaskName, $_.State,
                 $(if ($info) { $info.LastRunTime } else { '?' }),
                 $(if ($info) { $info.LastTaskResult } else { '?' }))
            Say ("     args: {0}" -f $act)
        }
    }
} catch { Say "  task query failed" }

Say "--- 启动项 ---"
Get-CimInstance Win32_StartupCommand -ErrorAction SilentlyContinue |
    Where-Object { $_.Command -match 'ygx' } |
    ForEach-Object { Say ("  {0} :: {1}" -f $_.Name, $_.Command) }

Say "=== done ==="
Write-Output "written: $out"
