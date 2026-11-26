# Clean up one-off scripts and logs I pushed to .200's Downloads.
# Keeps the reusable ones, deletes only named leftovers. Does not touch any
# exe/zip/business file. ASCII only: PowerShell 5.1 reads a BOM-less .ps1 as
# ANSI, so any non-ASCII text here would break parsing.
$dir = "C:\Users\Administrator\Downloads"

# Reusable: keep these.
$keep = @("peer_apply_update.ps1", "peer_install_autostart.ps1", "peer_launch.cmd")

# One-off: sent for a specific job, no reuse value.
$disposable = @(
    "rd_vpn_c1.ps1", "rd_upd_b1.ps1", "rd_upd_a2.ps1",
    "peer_vpn_check.ps1", "peer_vpn_check (1).ps1",
    "peer_apply_update (1).ps1", "peer_apply_update (2).ps1",
    "peer_update_now.ps1",
    "peer_load.ps1", "peer_who.ps1", "peer_ports.ps1",
    "peer_cpu.ps1", "peer_diag.ps1", "peer_procs.ps1",
    "peer_pileup.ps1", "peer_grow.ps1", "peer_ygx.ps1", "peer_py.ps1",
    "peer_edr.ps1", "peer_prefs_keys.ps1",
    "rd-update.log", "rd-update-now.log"
)

"=== keep ==="
foreach ($n in $keep) {
    $p = Join-Path $dir $n
    if (Test-Path $p) { "  keep  $n" }
}

"=== delete ==="
$freed = 0
foreach ($n in $disposable) {
    $p = Join-Path $dir $n
    if (-not (Test-Path $p)) { continue }
    $size = (Get-Item $p).Length
    Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path $p)) {
        "  del   $n ($size bytes)"
        $freed += $size
    } else {
        "  FAIL  $n"
    }
}
"released: $freed bytes"

"=== remaining ps1 / cmd / log ==="
Get-ChildItem $dir -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Extension -in ".ps1", ".cmd", ".log" } |
    Sort-Object Name |
    ForEach-Object { "  {0}  {1} bytes" -f $_.Name, $_.Length }
