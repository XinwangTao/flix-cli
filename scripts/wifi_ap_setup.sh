#!/usr/bin/env bash
# 配网模式：板子自己发一个 AP（默认 RelayDesk-Setup），手机/电脑连上来选要加入的 Wi-Fi。
#
#   sudo scripts/wifi_ap_setup.sh [ssid] [password] [ap-ip]
#
# 连上 AP 后打开 http://<ap-ip>/ ，页面列出周边 Wi-Fi；提交后板子切 STA，
# 连不上会自动退回 AP 模式（见 scripts/wifi_provision.py）。
set -euo pipefail

SSID="${1:-RelayDesk-Setup}"
PASS="${2:-relaydesk}"
AP_IP="${3:-10.42.0.1}"
IFACE="${IFACE:-${WIFI_IFACE:-wlan0}}"
DIR="${DIR:-/opt/flixcli}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "缺少依赖: $1" >&2; exit 2; }; }
need hostapd; need dnsmasq

# 1) 让出网卡：板上可能同时有 NM、board-wifi.service、D-Bus 激活的 supplicant
for unit in board-wifi.service wifi-wlan0.service; do
  systemctl stop "$unit" 2>/dev/null || true
done
if command -v nmcli >/dev/null 2>&1; then
  nmcli device set "$IFACE" managed no 2>/dev/null || true
fi
ip link set "$IFACE" down
ip addr flush dev "$IFACE"
pkill -x wpa_supplicant 2>/dev/null || true
sleep 2

# 2) 切 AP 前先扫一遍（hostapd 起来后不允许扫描）。
#    qcacld 的 iw scan 会卡在等新结果，用临时 supplicant 的 scan_results 更稳。
ip link set "$IFACE" up
cat > /run/wpa-scan.conf <<'CONF'
ctrl_interface=/run/wpa_supplicant
CONF
if command -v wpa_supplicant >/dev/null 2>&1; then
  wpa_supplicant -B -i "$IFACE" -D nl80211 -c /run/wpa-scan.conf -O /run/wpa_supplicant     >/dev/null 2>&1 || true
  sleep 2
  wpa_cli -p /run/wpa_supplicant -i "$IFACE" scan >/dev/null 2>&1 || true
  sleep 6
  wpa_cli -p /run/wpa_supplicant -i "$IFACE" scan_results 2>/dev/null > /run/wifi-scan.txt || true
  pkill -x wpa_supplicant 2>/dev/null || true
  sleep 1
fi
ip link set "$IFACE" down

# 3) 起 AP
cat > /run/hostapd-relaydesk.conf <<CONF
interface=$IFACE
driver=nl80211
ssid=$SSID
hw_mode=g
channel=6
wmm_enabled=1
auth_algs=1
wpa=2
wpa_passphrase=$PASS
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
ignore_broadcast_ssid=0
CONF

pkill -f hostapd-relaydesk 2>/dev/null || true
hostapd -B -P /run/hostapd-relaydesk.pid /run/hostapd-relaydesk.conf
sleep 1

# 4) 地址必须在 hostapd 之后配：驱动切 AP 模式时会清掉旧地址
ip addr flush dev "$IFACE"
ip addr add "$AP_IP/24" dev "$IFACE"
ip link set "$IFACE" up

# 5) DHCP（只做 DHCP，不抢 53 端口，系统 dnsmasq 还在跑）
cat > /run/dnsmasq-relaydesk.conf <<CONF
interface=$IFACE
bind-interfaces
port=0
dhcp-range=10.42.0.10,10.42.0.50,12h
dhcp-option=3,$AP_IP
dhcp-option=6,$AP_IP
dhcp-authoritative
dhcp-leasefile=/run/dnsmasq-relaydesk.leases
CONF
pkill -f dnsmasq-relaydesk 2>/dev/null || true
dnsmasq --conf-file=/run/dnsmasq-relaydesk.conf --pid-file=/run/dnsmasq-relaydesk.pid \
  || echo "dnsmasq 启动失败（页面仍可用，可手动设静态 IP）" >&2

# 6) 配网页面（python 标准库；80 常被 nginx 占，端口由脚本自己挑）
pkill -f wifi_provision.py 2>/dev/null || true
rm -f /run/wifi-provision.port
nohup python3 "$DIR/scripts/wifi_provision.py" --iface "$IFACE"       >>/var/log/wifi-provision.log 2>&1 &
sleep 2
PORT=$(cat /run/wifi-provision.port 2>/dev/null || echo "")
if [ "${PORT:-}" = "80" ] || [ -z "${PORT:-}" ]; then
  URL="http://$AP_IP/"
else
  URL="http://$AP_IP:$PORT/"
fi
echo "配网模式已启动：SSID=$SSID 密码=$PASS 页面 $URL"
