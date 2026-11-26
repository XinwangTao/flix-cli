<#
    只读：定位高占用进程的身份（python/grep 等）与两个 guard 脚本的内容概要。
#>
$out = Join-Path $env:TEMP "rd-who.txt"
"=== who $(Get-Date -Format o) ===" | Out-File -FilePath $out -Encoding UTF8
function Say($t) { $t | Out-File -FilePath $out -Append -Encoding UTF8 }

Say "--- Top 8 CPU 进程的完整命令行 ---"
$s1 = @{}
Get-Process | ForEach-Object { $s1[$_.Id] = $_.CPU }
Start-Sleep -Seconds 6
$rows = Get-Process | ForEach-Object {
    $old = $s1[$_.Id]
    if ($null -ne $old -and $null -ne $_.CPU) {
        [pscustomobject]@{
            Id = $_.Id
            Name = $_.ProcessName
            Cpu = [math]::Round((($_.CPU - $old) / 6) * 100, 1)
            MB = [math]::Round($_.WorkingSet64 / 1MB, 0)
        }
    }
} | Sort-Object Cpu -Descending | Select-Object -First 8

foreach ($row in $rows) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($row.Id)" -ErrorAction SilentlyContinue
    $cmd = if ($proc) { [string]$proc.CommandLine } else { '?' }
    if ($cmd.Length -gt 200) { $cmd = $cmd.Substring(0, 200) }
    Say ("{0,-18} pid={1,-7} cpu={2,5}% mem={3,5}MB :: {4}" -f $row.Name, $row.Id, $row.Cpu, $row.MB, $cmd)
    if ($proc) {
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.ParentProcessId)" -ErrorAction SilentlyContinue
        if ($parent) {
            $pcmd = [string]$parent.CommandLine
            if ($pcmd.Length -gt 160) { $pcmd = $pcmd.Substring(0, 160) }
            Say ("    父: {0} pid={1} :: {2}" -f $parent.Name, $parent.ProcessId, $pcmd)
        }
    }
}

Say "--- 两个 guard 脚本的头部（各 25 行） ---"
foreach ($path in @('D:\_diag\resource_guard.ps1',
                    'C:\Users\Administrator\AppData\Local\PCCrashGuard\guard.ps1')) {
    Say ("### {0}" -f $path)
    if (Test-Path $path) {
        $item = Get-Item $path
        Say ("   大小 {0} 字节，改动 {1}" -f $item.Length, $item.LastWriteTime)
        Get-Content $path -TotalCount 25 -ErrorAction SilentlyContinue |
            ForEach-Object { Say ("   | " + $_) }
    } else {
        Say "   (文件不存在)"
    }
}

Say "--- python 进程清单（按内存） ---"
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    ForEach-Object {
        $ws = (Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue).WorkingSet64
        $mb = if ($ws) { [math]::Round($ws / 1MB, 0) } else { 0 }
        $cmd = [string]$_.CommandLine
        if ($cmd.Length -gt 110) { $cmd = $cmd.Substring(0, 110) }
        Say ("pid={0} mem={1}MB :: {2}" -f $_.ProcessId, $mb, $cmd)
    }

Say "=== done ==="
Write-Output "written: $out"
