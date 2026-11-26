<#
    只读：90 秒采样那个内存失控进程，量化增长速度。
#>
$out = Join-Path $env:TEMP "rd-grow.txt"
"=== grow $(Get-Date -Format o) ===" | Out-File -FilePath $out -Encoding UTF8
function Say($t) { $t | Out-File -FilePath $out -Append -Encoding UTF8 }

for ($i = 1; $i -le 5; $i++) {
    $targets = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'ygx_consumer' }
    if (-not $targets) {
        Say ("[{0}] 没有在跑" -f (Get-Date -Format HH:mm:ss))
    } else {
        foreach ($t in $targets) {
            $live = Get-Process -Id $t.ProcessId -ErrorAction SilentlyContinue
            $mb = if ($live) { [math]::Round($live.WorkingSet64 / 1MB, 0) } else { -1 }
            Say ("[{0}] pid={1} mem={2}MB" -f (Get-Date -Format HH:mm:ss), $t.ProcessId, $mb)
        }
    }
    if ($i -lt 5) { Start-Sleep -Seconds 25 }
}

$os = Get-CimInstance Win32_OperatingSystem
Say ("总内存 {0}MB，空闲 {1}MB" -f
     [math]::Round($os.TotalVisibleMemorySize / 1KB, 0),
     [math]::Round($os.FreePhysicalMemory / 1KB, 0))
Say "=== done ==="
Write-Output "written: $out"
