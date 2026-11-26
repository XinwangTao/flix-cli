$out = Join-Path $env:TEMP "rd-cpu.txt"
"=== cpu $(Get-Date -Format o) ===" | Out-File -FilePath $out -Encoding UTF8
$s1 = Get-Process | Select-Object Id,ProcessName,CPU
Start-Sleep -Seconds 8
$s2 = Get-Process | Select-Object Id,ProcessName,CPU
$byId = @{}
foreach ($p in $s1) { $byId[$p.Id] = $p }
$rows = foreach ($p in $s2) {
    $old = $byId[$p.Id]
    if ($old -and $p.CPU -and $old.CPU) {
        [pscustomobject]@{ Name = $p.ProcessName; Id = $p.Id;
                           CpuPct = [math]::Round((($p.CPU - $old.CPU) / 8) * 100, 1) }
    }
}
$rows | Sort-Object CpuPct -Descending | Select-Object -First 12 |
    ForEach-Object { ("{0,-22} pid={1,-7} cpu={2}%" -f $_.Name, $_.Id, $_.CpuPct) } |
    Out-File -FilePath $out -Append -Encoding UTF8
"cpu=$(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average | Select-Object -ExpandProperty Average)%" |
    Out-File -FilePath $out -Append -Encoding UTF8
Write-Output "written: $out"