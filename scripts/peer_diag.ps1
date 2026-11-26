<#
    对端整机卡死取证（只读）。把结果写到一个文本文件，供远程读取。
    不发现在机器上，只读 EventLog / 磁盘 / 内存 / 驱动。
#>
$out = Join-Path $env:TEMP "rd-diag.txt"
"=== diag $(Get-Date -Format o) ===" | Out-File -FilePath $out -Encoding UTF8

function Say($t) { $t | Out-File -FilePath $out -Append -Encoding UTF8 }

Say "--- boot / uptime ---"
Say ((Get-CimInstance Win32_OperatingSystem).LastBootUpTime | Out-String).Trim()
Say ("freeMemMB={0} totalMemMB={1}" -f `
    [math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory/1KB,0), `
    [math]::Round((Get-CimInstance Win32_OperatingSystem).TotalVisibleMemorySize/1KB,0))

Say "--- disk ---"
try {
    Get-PhysicalDisk | ForEach-Object {
        Say ("{0} health={1} op={2} sizeGB={3}" -f $_.FriendlyName, $_.HealthStatus,
             $_.OperationalStatus, [math]::Round($_.Size/1GB,0))
    }
} catch { Say "disk query failed: $($_.Exception.Message)" }

Say "--- event log (last 24h, level 1-3, top 40) ---"
try {
    Get-WinEvent -FilterHashtable @{LogName='System'; Level=1,2,3; StartTime=(Get-Date).AddDays(-1)} `
        -MaxEvents 40 -ErrorAction Stop |
        ForEach-Object {
            $msg = ($_.Message -replace '\s+', ' ')
            if ($msg.Length -gt 150) { $msg = $msg.Substring(0,150) }
            Say ("{0} id={1} {2} :: {3}" -f $_.TimeCreated.ToString('MM-dd HH:mm:ss'),
                 $_.Id, $_.ProviderName, $msg)
        }
} catch { Say "eventlog failed: $($_.Exception.Message)" }

Say "--- disk/ntfs specific ---"
try {
    Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=(Get-Date).AddDays(-2)} `
        -MaxEvents 400 -ErrorAction Stop |
        Where-Object { $_.ProviderName -match 'disk|Ntfs|storahci|volmgr|Kernel-Power' } |
        Select-Object -First 20 |
        ForEach-Object {
            $msg = ($_.Message -replace '\s+', ' ')
            if ($msg.Length -gt 120) { $msg = $msg.Substring(0,120) }
            Say ("{0} id={1} {2} :: {3}" -f $_.TimeCreated.ToString('MM-dd HH:mm:ss'),
                 $_.Id, $_.ProviderName, $msg)
        }
} catch { Say "disk events failed: $($_.Exception.Message)" }

Say "--- sangfor ---"
try {
    Get-Service Sangfor* -ErrorAction SilentlyContinue | ForEach-Object { Say ("svc {0} = {1}" -f $_.Name, $_.Status) }
    Get-CimInstance Win32_SystemDriver -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match 'sangfor|^sf' } |
        ForEach-Object { Say ("drv {0} state={1} started={2}" -f $_.Name, $_.State, $_.Started) }
} catch { Say "sangfor failed: $($_.Exception.Message)" }

Say "--- top processes ---"
Get-Process | Group-Object ProcessName | Sort-Object Count -Descending |
    Select-Object -First 8 | ForEach-Object { Say ("{0} x{1}" -f $_.Name, $_.Count) }

Say "--- recent hotfix ---"
try {
    Get-HotFix -ErrorAction Stop | Sort-Object InstalledOn -Descending |
        Select-Object -First 3 | ForEach-Object { Say ("{0} {1}" -f $_.HotFixID, $_.InstalledOn) }
} catch { Say "hotfix failed" }

Say "=== done ==="
Write-Output "written: $out"
