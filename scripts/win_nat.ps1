$out = 'F:\projects\_agent-runs\nat_report.txt'
function Log($m) { $m | Out-File -Append -Encoding utf8 $out }
Remove-Item $out -ErrorAction SilentlyContinue
Log ("=== NAT fix " + (Get-Date -Format 'HH:mm:ss') + " ===")
try {
  Set-NetIPInterface -InterfaceAlias '以太网' -Forwarding Enabled -ErrorAction Stop
  Set-NetIPInterface -InterfaceAlias 'WLAN' -Forwarding Enabled -ErrorAction Stop
  Log "forwarding enabled on 以太网 / WLAN"
} catch { Log ("forwarding ERR: " + $_.Exception.Message) }
try {
  Get-NetNat -ErrorAction SilentlyContinue | Remove-NetNat -Confirm:$false -ErrorAction SilentlyContinue
  New-NetNat -Name 'boardnat' -InternalIPInterfaceAddressPrefix 192.168.137.0/24 -ErrorAction Stop | Out-Null
  Log "NetNat boardnat 192.168.137.0/24 created"
} catch { Log ("nat ERR: " + $_.Exception.Message) }
try {
  Get-NetNat | ForEach-Object { Log ("NAT: " + $_.Name + " " + $_.InternalIPInterfaceAddressPrefix) }
  Get-NetIPInterface -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -in @('WLAN','以太网') } |
    ForEach-Object { Log ("iface " + $_.InterfaceAlias + " forwarding=" + $_.Forwarding) }
} catch { Log ("report ERR: " + $_.Exception.Message) }
