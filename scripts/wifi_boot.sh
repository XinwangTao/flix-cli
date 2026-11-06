#!/usr/bin/env bash
# 开机决策：优先用已有配置连 STA，30 秒连不上就退到 AP 配网模式。
#
#   sudo scripts/wifi_boot.sh [等待秒数]
set -uo pipefail

WAIT="${1:-30}"
DIR="${DIR:-/opt/flixcli}"
CONF="/etc/wpa_supplicant.conf"

has_conf=0
[ -s "$CONF" ] && grep -q "ssid=" "$CONF" && has_conf=1

if [ "$has_conf" = "1" ]; then
  systemctl stop wifi-provision 2>/dev/null || true
  systemctl --no-block restart wifi-sta 2>/dev/null || true
  for _ in $(seq 1 "$WAIT"); do
    sleep 1
    if wpa_cli -p /run/wpa_supplicant -i wlan0 status 2>/dev/null | grep -q "wpa_state=COMPLETED"; then
      echo "已连上 Wi-Fi（$CONF），保持 STA 模式"
      exit 0
    fi
  done
  echo "STA 连接超时，切到配网模式"
  systemctl --no-block stop wifi-sta 2>/dev/null || true
fi

# 直接 setsid 脱离本 unit 的 cgroup：否则 unit 结束时 hostapd/dnsmasq 会被一起杀掉
systemctl stop wifi-provision 2>/dev/null || true
setsid nohup bash "$DIR/scripts/wifi_ap_setup.sh" >>/var/log/wifi-ap.log 2>&1 &
sleep 2
echo "已切到 AP 配网模式"
