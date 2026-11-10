#!/usr/bin/env bash
# Bluetooth PAN (NAP) fallback link for boards without usable Wi-Fi.
#
#   sudo scripts/bt_pan.sh [iface-ip] [nap-name]
#
# Brings up the first HCI adapter, advertises a NAP network and serves DHCP on
# bnep0 so a PC that pairs with the board gets an IP and can SSH / run RelayDesk
# over Bluetooth — no cable, no access point.
#
# Requires: bluez (bluetoothctl), bluez-tools (bt-network) or `bt-pan`,
# dnsmasq, and an HCI adapter that actually answers (USB dongles do; the
# onboard QCA chip on the QCM6490 devkit currently does not — see docs/bt-pan.md).
set -euo pipefail

IP="${1:-10.55.0.1}"
NAP="${2:-RelayDesk-BT}"
BRIDGE="bnep0"

need() { command -v "$1" >/dev/null 2>&1 || { echo "缺少依赖: $1" >&2; exit 2; }; }
need bluetoothctl
need dnsmasq

if ! hciconfig 2>/dev/null | grep -q "Bus: UART\|Bus: USB"; then
  echo "没有可用的蓝牙控制器（hci0 未就绪）。" >&2
  echo "板载 QCA 芯片当前不应答 HCI；插一个 USB 蓝牙适配器后重试。" >&2
  exit 3
fi

systemctl start bluetooth
hciconfig hci0 up || true
bluetoothctl power on >/dev/null
bluetoothctl discoverable on >/dev/null
bluetoothctl pairable on >/dev/null

# PAN server: bluez-tools ships bt-network, newer images ship bt-pan.
if command -v bt-network >/dev/null 2>&1; then
  pkill -f "bt-network -s nap" 2>/dev/null || true
  bt-network -s nap "$NAP" >/var/log/bt-pan.log 2>&1 &
elif command -v bt-pan >/dev/null 2>&1; then
  pkill -f "bt-pan server" 2>/dev/null || true
  bt-pan server "$BRIDGE" >/var/log/bt-pan.log 2>&1 &
else
  echo "缺少 bt-network/bt-pan（bluez-tools 或 bluez-tools-extra）" >&2
  exit 2
fi

# Wait for the bridge to appear, then give the peer an address.
for _ in $(seq 1 30); do
  if ip link show "$BRIDGE" >/dev/null 2>&1; then break; fi
  sleep 1
done
ip link show "$BRIDGE" >/dev/null 2>&1 || { echo "bnep0 未建立，PC 需要先连上 PAN" >&2; exit 4; }

ip addr flush dev "$BRIDGE"
ip addr add "$IP/24" dev "$BRIDGE"
ip link set "$BRIDGE" up

pkill -f "dnsmasq.*$BRIDGE" 2>/dev/null || true
dnsmasq --interface="$BRIDGE" --bind-interfaces --dhcp-range=10.55.0.10,10.55.0.50,12h \
        --pid-file=/run/dnsmasq-btpan.pid --log-facility=/var/log/dnsmasq-btpan.log

echo "PAN 就绪: $NAP，板子地址 $IP，PC 连上后会自动拿到 10.55.0.x"
