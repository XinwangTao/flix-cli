# Clean stale PyInstaller _MEI* dirs on the peer. ASCII only.
# Keeps any dir that matches a running RelayDesk process start time (+-3 min).
$temp = $env:TEMP
$cutoff = (Get-Date).AddHours(-12)

$protected = @{}
foreach ($p in (Get-Process RelayDesk -ErrorAction SilentlyContinue)) {
    foreach ($d in (Get-ChildItem $temp -Directory -Filter "_MEI*" -ErrorAction SilentlyContinue)) {
        if ($d.LastWriteTime -ge $p.StartTime.AddMinutes(-3) -and
            $d.LastWriteTime -le $p.StartTime.AddMinutes(3)) {
            $protected[$d.Name] = $p.Id
        }
    }
}
"protected: " + (($protected.Keys) -join ", ")

$deleted = 0
$freed = 0
$kept = 0
foreach ($d in (Get-ChildItem $temp -Directory -Filter "_MEI*" -ErrorAction SilentlyContinue)) {
    if ($protected.ContainsKey($d.Name)) { $kept++; continue }
    if ($d.LastWriteTime -ge $cutoff) { $kept++; continue }
    $sz = (Get-ChildItem $d.FullName -Recurse -File -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
    try {
        Remove-Item $d.FullName -Recurse -Force -ErrorAction Stop
        $deleted++
        $freed += $sz
    } catch {
        $kept++
    }
}
"deleted {0} dirs, freed {1:N0} MB, kept {2}" -f $deleted, ($freed/1MB), $kept
$drive = (Get-Item $temp).PSDrive
"{0}: {1:N1} GB free now" -f $drive.Name, ($drive.Free/1GB)
