<#
    查进程父子关系，找出 conhost/powershell/cmd 是不是被泄漏（只读）。
#>
$out = Join-Path $env:TEMP "rd-procs.txt"
"=== procs $(Get-Date -Format o) ===" | Out-File -FilePath $out -Encoding UTF8

function Say($t) { $t | Out-File -FilePath $out -Append -Encoding UTF8 }

Say "--- counts ---"
Get-Process | Group-Object ProcessName |
    Sort-Object Count -Descending | Select-Object -First 12 |
    ForEach-Object { Say ("{0} x{1}" -f $_.Name, $_.Count) }

Say "--- powershell / cmd / conhost / python / node leftovers ---"
$all = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
$byId = @{}
foreach ($p in $all) { $byId[[int]$p.ProcessId] = $p }

foreach ($name in @('powershell.exe','cmd.exe','conhost.exe','python.exe','node.exe','claude.exe')) {
    $rows = @($all | Where-Object { $_.Name -ieq $name })
    if ($rows.Count -eq 0) { continue }
    Say ("### {0}: {1}" -f $name, $rows.Count)
    foreach ($row in ($rows | Select-Object -First 6)) {
        $parent = $byId[[int]$row.ParentProcessId]
        $pname = if ($parent) { $parent.Name } else { '?' }
        $cmd = [string]$row.CommandLine
        if ($cmd.Length -gt 100) { $cmd = $cmd.Substring(0,100) }
        Say ("  pid={0} ppid={1}({2}) {3}" -f $row.ProcessId, $row.ParentProcessId, $pname, $cmd)
    }
}

Say "--- 按父进程统计 ---"
$all | Group-Object ParentProcessId | Sort-Object Count -Descending |
    Select-Object -First 6 | ForEach-Object {
        $parent = $byId[[int]$_.Name]
        $pname = if ($parent) { $parent.Name } else { 'gone' }
        Say ("ppid={0}({1}) children={2}" -f $_.Name, $pname, $_.Count)
    }

Say "--- RelayDesk 相关进程 ---"
$all | Where-Object { $_.Name -match 'RelayDesk|flix' } | ForEach-Object {
    $cmd = [string]$_.CommandLine
    if ($cmd.Length -gt 110) { $cmd = $cmd.Substring(0,110) }
    Say ("  pid={0} ppid={1} {2}" -f $_.ProcessId, $_.ParentProcessId, $cmd)
}

Say "=== done ==="
Write-Output "written: $out"
