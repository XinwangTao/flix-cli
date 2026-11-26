<#
    查「进程为什么堆积」：powershell/cmd 的存活时长 + 计划任务最近运行时间（只读）。
#>
$out = Join-Path $env:TEMP "rd-pileup.txt"
"=== pileup $(Get-Date -Format o) ===" | Out-File -FilePath $out -Encoding UTF8
function Say($t) { $t | Out-File -FilePath $out -Append -Encoding UTF8 }

Say "--- powershell/cmd/conhost 存活时长 ---"
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -in @('powershell.exe','cmd.exe','conhost.exe') } |
    ForEach-Object {
        $age = if ($_.CreationDate) {
            [math]::Round(((Get-Date) - $_.CreationDate).TotalMinutes, 0)
        } else { -1 }
        $cmd = [string]$_.CommandLine
        if ($cmd.Length -gt 90) { $cmd = $cmd.Substring(0, 90) }
        Say ("{0} pid={1} ageMin={2} {3}" -f $_.Name, $_.ProcessId, $age, $cmd)
    } | Out-Null

Say "--- 运行中且重复间隔 <= 15 分钟的计划任务 ---"
try {
    Get-ScheduledTask -ErrorAction Stop | ForEach-Object {
        $task = $_
        foreach ($trigger in $task.Triggers) {
            $interval = $trigger.Repetition.Interval
            if ($interval -and ($interval -match 'PT([1-9]|1[0-5])M|PT[1-9][0-9]*S')) {
                $info = $null
                try { $info = Get-ScheduledTaskInfo -TaskName $task.TaskName -TaskPath $task.TaskPath -ErrorAction SilentlyContinue } catch {}
                Say ("{0}{1} every={2} last={3} next={4} state={5}" -f
                     $task.TaskPath, $task.TaskName, $interval,
                     $(if ($info) { $info.LastRunTime } else { '?' }),
                     $(if ($info) { $info.NextRunTime } else { '?' }),
                     $task.State)
            }
        }
    }
} catch { Say "scheduled task query failed: $($_.Exception.Message)" }

Say "--- GitHub Actions runner ---"
Get-Process -Name 'Runner.Listener','Runner.Worker' -ErrorAction SilentlyContinue |
    ForEach-Object { Say ("{0} pid={1} start={2}" -f $_.ProcessName, $_.Id, $_.StartTime) }
Get-Service 'actions.runner*' -ErrorAction SilentlyContinue |
    ForEach-Object { Say ("svc {0} = {1}" -f $_.Name, $_.Status) }

Say "=== done ==="
Write-Output "written: $out"
