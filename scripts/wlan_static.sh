#!/usr/bin/env bash
# wlan0 地址：有静态配置就用静态，否则回退 DHCP。
#   /etc/relaydesk/wlan.conf
#     STATIC=1
#     IP=192.168.43.50/24
#     GW=192.168.43.1
#     DNS=192.168.43.1
# 典型用途：Mac 用手机热点调试板子时，不想每次查 DHCP 地址。
set -u
CONF="${WLAN_CONF:-/etc/relaydesk/wlan.conf}"
IF="${IFACE:-wlan0}"

if [ -f "$CONF" ]; then
  # shellcheck disable=SC1090
  . "$CONF"
fi

if [ "${STATIC:-0}" = "1" ] && [ -n "${IP:-}" ]; then
  ip addr flush dev "$IF" 2>/dev/null || true
  ip addr add "$IP" dev "$IF" || exit 1
  ip link set "$IF" up
  if [ -n "${GW:-}" ]; then
    ip route replace default via "$GW" dev "$IF" metric 300
  fi
  if [ -n "${DNS:-}" ]; then
    printf 'nameserver %s\n' "$DNS" > /etc/resolv.conf
  fi
  echo "wlan0 静态地址 $IP${GW:+ 网关 $GW}"
  exit 0
fi

# 没配静态就走 DHCP，最多重试约 1 分钟（AP 刚起来时 DHCP 常要等一会）
for _ in $(seq 1 20); do
  /sbin/udhcpc -i "$IF" -n -q -t 3 -T 2 >/dev/null 2>&1 && exit 0
  sleep 3
done
echo "wlan0 DHCP 未取到地址" >&2
exit 1
