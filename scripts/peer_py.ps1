<#
    只读：定位那个内存失控的 python 进程（谁起的、跑什么脚本、还有谁在涨）。
#>
$out = Join-Path $env:TEMP "rd-py.txt"
"=== py $(Get-Date -Format o) ===" | Out-File -FilePath $out -Encoding UTF8
function Say($t) { $t | Out-File -FilePath $out -Append -Encoding UTF8 }

Say "--- python / pythonw 进程（按内存降序） ---"
$procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe' }
foreach ($p in $procs) {
    $live = Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue
    $mb = if ($live) { [math]::Round($live.WorkingSet64 / 1MB, 0) } else { -1 }
    $cmd = [string]$p.CommandLine
    Say ("pid={0,-7} ppid={1,-7} mem={2,6}MB" -f $p.ProcessId, $p.ParentProcessId, $mb)
    Say ("   cmd: {0}" -f $cmd)
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.ParentProcessId)" -ErrorAction SilentlyContinue
    if ($parent) {
        Say ("   父: {0} pid={1} :: {2}" -f $parent.Name, $parent.ProcessId, [string]$parent.CommandLine)
    }
}

Say "--- 内存 Top 10（含所有进程） ---"
Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 10 |
    ForEach-Object { Say ("{0,-18} pid={1,-7} {2,6}MB" -f $_.ProcessName, $_.Id, [math]::Round($_.WorkingSet64 / 1MB, 0)) }

Say "--- 30 秒内存趋势（只看 >500MB 的） ---"
$before = @{}
Get-Process | ForEach-Object { $before[$_.Id] = $_.WorkingSet64 }
Start-Sleep -Seconds 30
Get-Process | ForEach-Object {
    $old = $before[$_.Id]
    if ($old) {
        $deltaMB = [math]::Round(($_.WorkingSet64 - $old) / 1MB, 0)
        if ([math]::Abs($deltaMB) -ge 50 -or $_.WorkingSet64 -gt 500MB) {
            Say ("{0,-18} pid={1,-7} now={2,6}MB delta={3,+6}MB" -f
                 $_.ProcessName, $_.Id, [math]::Round($_.WorkingSet64 / 1MB, 0), $deltaMB)
        }
    }
}

Say "=== done ==="
Write-Output "written: $out"
