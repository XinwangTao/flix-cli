<#
    只读：追查对端「反复整机冻结」的负载来源。
    不 kill、不 stop、不改任何东西。
#>
$out = Join-Path $env:TEMP "rd-load.txt"
"=== load $(Get-Date -Format o) ===" | Out-File -FilePath $out -Encoding UTF8
function Say($t) { $t | Out-File -FilePath $out -Append -Encoding UTF8 }

Say "--- 8 秒 CPU 采样（进程级） ---"
$s1 = @{}
Get-Process | ForEach-Object { $s1[$_.Id] = $_.CPU }
Start-Sleep -Seconds 8
$rows = Get-Process | ForEach-Object {
    $old = $s1[$_.Id]
    if ($null -ne $old -and $null -ne $_.CPU) {
        [pscustomobject]@{
            Name = $_.ProcessName
            Id = $_.Id
            Cpu = [math]::Round((($_.CPU - $old) / 8) * 100, 1)
            MB = [math]::Round($_.WorkingSet64 / 1MB, 0)
        }
    }
}
$rows | Sort-Object Cpu -Descending | Select-Object -First 12 |
    ForEach-Object { Say ("{0,-22} pid={1,-7} cpu={2,5}%  mem={3}MB" -f $_.Name, $_.Id, $_.Cpu, $_.MB) }
Say ("总 CPU = {0}%" -f (Get-CimInstance Win32_Processor |
    Measure-Object -Property LoadPercentage -Average |
    Select-Object -ExpandProperty Average))

Say "--- 所有 grep / sed / awk / rg / findstr 进程及其父进程 ---"
$all = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
$byId = @{}
foreach ($p in $all) { $byId[[int]$p.ProcessId] = $p }
$all | Where-Object { $_.Name -match '^(grep|sed|awk|rg|findstr|find|7z|tar)\.exe$' } |
    ForEach-Object {
        $parent = $byId[[int]$_.ParentProcessId]
        $pname = if ($parent) { $parent.Name } else { '?' }
        $cmd = [string]$_.CommandLine
        if ($cmd.Length -gt 120) { $cmd = $cmd.Substring(0, 120) }
        Say ("{0} pid={1} ppid={2}({3}) :: {4}" -f $_.Name, $_.ProcessId,
             $_.ParentProcessId, $pname, $cmd)
        if ($parent) {
            $pcmd = [string]$parent.CommandLine
            if ($pcmd.Length -gt 140) { $pcmd = $pcmd.Substring(0, 140) }
            Say ("    父进程: {0} :: {1}" -f $parent.Name, $pcmd)
        }
    }

Say "--- 所有含 resource_guard / guard 的进程 ---"
$all | Where-Object { $_.CommandLine -match 'guard' } |
    ForEach-Object {
        $cmd = [string]$_.CommandLine
        if ($cmd.Length -gt 150) { $cmd = $cmd.Substring(0, 150) }
        Say ("pid={0} ppid={1} {2} :: {3}" -f $_.ProcessId, $_.ParentProcessId,
             $_.Name, $cmd)
    }

Say "--- 长时间运行的 powershell（>30 分钟）---"
$now = Get-Date
$all | Where-Object { $_.Name -eq 'powershell.exe' -and $_.CreationDate } |
    ForEach-Object {
        $mins = [math]::Round(($now - $_.CreationDate).TotalMinutes, 0)
        if ($mins -gt 30) {
            $cmd = [string]$_.CommandLine
            if ($cmd.Length -gt 120) { $cmd = $cmd.Substring(0, 120) }
            Say ("pid={0} age={1}min :: {2}" -f $_.ProcessId, $mins, $cmd)
        }
    }

Say "--- 相关计划任务 ---"
try {
    Get-ScheduledTask -ErrorAction Stop | ForEach-Object {
        $task = $_
        $args = ($task.Actions | ForEach-Object { $_.Arguments }) -join ' | '
        if ($args -match 'guard|actions-runner|RelayDesk') {
            $info = $null
            try { $info = Get-ScheduledTaskInfo -TaskName $task.TaskName -TaskPath $task.TaskPath -ErrorAction SilentlyContinue } catch {}
            Say ("{0} state={1} last={2} next={3}" -f $task.TaskName, $task.State,
                 $(if ($info) { $info.LastRunTime } else { '?' }),
                 $(if ($info) { $info.NextRunTime } else { '?' }))
            Say ("   args: {0}" -f $(if ($args.Length -gt 130) { $args.Substring(0, 130) } else { $args }))
        }
    }
} catch { Say "scheduled task query failed: $($_.Exception.Message)" }

Say "--- 内存/句柄压力 ---"
$os = Get-CimInstance Win32_OperatingSystem
Say ("freeMB={0} totalMB={1}" -f [math]::Round($os.FreePhysicalMemory / 1KB, 0),
     [math]::Round($os.TotalVisibleMemorySize / 1KB, 0))
Say ("进程总数={0}" -f @(Get-Process).Count)

Say "=== done ==="
Write-Output "written: $out"
