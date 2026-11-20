"""UDP multicast discovery (224.0.0.168:8891).

Wire format (captured from official client, see reverse/capture_discovery.txt):
the datagram payload is a JSON-encoded STRING whose content is itself JSON:

    "{\"deviceModal\":{\"alias\":...,\"deviceModel\":\"Windows\",\"deviceType\":\"desktop\",
      \"fingerprint\":\"<uuid>\",\"port\":8891,\"version\":127,\"ip\":\"\",\"host\":\"\",
      \"from\":\"\",\"os\":\"windows\"}}"

On startup the official client bursts 11 identical announces. ip/host/from are
empty on the wire; receivers take the peer IP from the datagram source address.
Official clients only announce at startup — there is no query/response and no
mDNS service; already-running peers are found by probing TCP 8891.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import socket
import struct
import sys
from typing import Callable

from . import (ANNOUNCE_BURST, DEVICE_PORT, MULTICAST_GROUP, _win_ipv4_table,
               default_iface_ip)


def build_announce(ident: dict, port: int = DEVICE_PORT, app_version: int = 1) -> bytes:
    inner = {
        "deviceModal": {
            "alias": ident.get("alias", ""),
            "deviceModel": ident.get("deviceModel", "Windows"),
            "deviceType": ident.get("deviceType", "desktop"),
            "fingerprint": ident.get("fingerprint", ""),
            "port": port,
            "version": app_version,
            "ip": "",
            "host": "",
            "from": "",
            "os": ident.get("os", "windows"),
        }
    }
    return json.dumps(json.dumps(inner, separators=(",", ":")),
                      separators=(",", ":")).encode("utf-8")


def parse_datagram(data: bytes, src_ip: str) -> dict | None:
    """Return device dict or None if not a valid announce."""
    try:
        outer = json.loads(data.decode("utf-8"))
        if isinstance(outer, str):
            outer = json.loads(outer)
        modal = outer.get("deviceModal") if isinstance(outer, dict) else None
        if not isinstance(modal, dict) or not modal.get("fingerprint"):
            return None
        dev = dict(modal)
        if not dev.get("ip"):
            dev["ip"] = src_ip
        dev["source"] = src_ip
        return dev
    except (ValueError, UnicodeDecodeError):
        return None


class Discovery:
    """Listens for announces and can burst our own.

    iface_ip pins the multicast interface (join + send). None auto-detects via
    default_iface_ip(), which avoids VPN/virtual /32 adapters stealing the route.

    Announces also go out as LAN broadcast (subnet-directed + 255.255.255.255):
    on hosts whose security agents silently swallow outbound multicast [RT
    2026-10-22, pktmon Tx=0] this is the only emission that reaches the NIC.
    Receivers bind 0.0.0.0:DEVICE_PORT for discovery, so they pick broadcasts
    up without any change; duplicates are deduped per fingerprint.
    """

    def __init__(self, on_device: Callable[[dict], None],
                 iface_ip: str | None = None):
        self.on_device = on_device
        self.iface = iface_ip or default_iface_ip() or None
        self.transport: asyncio.DatagramTransport | None = None
        self.bound = False

    def _open(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # macOS/BSD needs SO_REUSEPORT (not just SO_REUSEADDR) for a second
        # listener on the same UDP port. Not every peer sets it, so this is a
        # best-effort coexistence attempt; scan_lan() is the guaranteed
        # fallback when it still fails.
        with contextlib.suppress(AttributeError, OSError):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.bind(("", DEVICE_PORT))
        mreq = (socket.inet_aton(MULTICAST_GROUP)
                + socket.inet_aton(self.iface or "0.0.0.0"))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        s.setblocking(False)
        return s

    async def start(self) -> bool:
        loop = asyncio.get_running_loop()
        try:
            sock = self._open()
        except OSError as e:
            print(f"[discovery] cannot bind UDP {DEVICE_PORT}"
                  f" (interface {self.iface or 'default'}): {e}",
                  file=sys.stderr, flush=True)
            return False
        self.transport, _ = await loop.create_datagram_endpoint(
            lambda: _Protocol(self.on_device), sock=sock)
        self.bound = True
        return True

    def _broadcast_addrs(self) -> list[str]:
        """Subnet-directed broadcast first (pinned to the multicast NIC), then
        the limited broadcast as a catch-all."""
        addrs: list[str] = []
        if self.iface:
            try:
                n = struct.unpack(">I", socket.inet_aton(self.iface))[0]
                for ip, mask in _win_ipv4_table():
                    if ip == self.iface and mask != "255.255.255.255":
                        m = struct.unpack(">I", socket.inet_aton(mask))[0]
                        addrs.append(socket.inet_ntoa(
                            struct.pack(">I", n | (~m & 0xFFFFFFFF))))
                        break
            except OSError:
                pass
        addrs.append("255.255.255.255")
        return addrs

    def announce_unicast(self, ident: dict, ips: list[str],
                         port: int = DEVICE_PORT) -> int:
        """把公告逐个单播给候选 IP。

        官方客户端只在启动时组播一次，之后既不回应探测也不定期公告；但它的
        UDP 8891 监听在收到单播合法公告时仍会处理。主动扫描后补一轮单播，
        官方端（任意版本）就会把我们列进设备列表。
        """
        payload = build_announce(ident, port=port)
        sent = 0
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for ip in ips:
                with contextlib.suppress(OSError):
                    tx.sendto(payload, (ip, DEVICE_PORT))
                    sent += 1
        finally:
            tx.close()
        return sent

    def announce(self, ident: dict, times: int = ANNOUNCE_BURST,
                 port: int = DEVICE_PORT) -> None:
        payload = build_announce(ident, port=port)
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 8)
            if self.iface:
                tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                              socket.inet_aton(self.iface))
            tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            # Wire dst port is DEVICE_PORT [RT capture_discovery.txt]: every
            # listener (official + flixcli) binds 8891 for discovery regardless
            # of its TCP channel port; `port` only labels the payload modal.
            targets = [(MULTICAST_GROUP, DEVICE_PORT)]
            targets += [(bc, DEVICE_PORT) for bc in self._broadcast_addrs()]
            for _ in range(times):
                for dst in targets:
                    with contextlib.suppress(OSError):
                        tx.sendto(payload, dst)
        finally:
            tx.close()

    async def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None
            self.bound = False


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, on_device: Callable[[dict], None]):
        self.on_device = on_device

    def datagram_received(self, data: bytes, addr) -> None:
        dev = parse_datagram(data, addr[0])
        if dev:
            self.on_device(dev)

    def error_received(self, exc: Exception) -> None:
        print(f"[discovery] {exc}", file=sys.stderr, flush=True)


# A single sweep must stay bounded: a corporate /16 NIC would otherwise turn
# one 60-second tick into 65k TCP connects. /22 (1022 hosts) is the widest
# shape a home/office LAN realistically uses; anything wider is trimmed to the
# /24 around our own address.
MAX_SWEEP_HOSTS = 1024


def _subnet_hosts(ip: str, mask: str, budget: int) -> list[str]:
    if budget <= 0 or not mask:
        return []
    try:
        net = ipaddress.ip_network(f"{ip}/{mask}", strict=False)
    except ValueError:
        return []
    if net.prefixlen < 22:
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
    return [str(host) for host in list(net.hosts())[:budget]]


def _physical_rows() -> list[tuple[str, str]]:
    """(address, mask) for every adapter that can carry LAN traffic."""
    from . import _win_ipv4_table, is_virtual_ip, virtual_interfaces
    try:
        rows = _win_ipv4_table()
    except Exception:  # noqa: BLE001 - discovery must never fail to start
        rows = []
    virtual = {ip for ip, _name in virtual_interfaces()}
    out: list[tuple[str, str]] = []
    for ip, mask in rows:
        if not ip or ip in virtual or is_virtual_ip(ip):
            continue
        if ip.startswith(("127.", "169.254.")):
            continue
        if mask in ("255.255.255.255", "255.255.255.254"):
            continue
        out.append((ip, mask))
    if not out:
        primary = default_iface_ip()
        if primary:
            out.append((primary, "255.255.255.0"))
    return out


def local_hosts(iface_ip: str | None = None,
                limit: int = MAX_SWEEP_HOSTS) -> list[str]:
    """Candidate hosts on every physical subnet, plus this machine.

    Two shapes were missed by the old "primary /24, minus our own address"
    rule: a /23 home LAN puts phones on the other half (192.168.236.x while we
    are 192.168.237.7), and the official Flix client on this very machine only
    answers at our own address or 127.0.0.1. Both are included now; our own
    fingerprint is still filtered by scan_lan, so probing ourselves is safe.
    """
    targets: list[str] = []
    seen: set[str] = set()

    def add(ip: str) -> None:
        if ip and ip not in seen and len(targets) < limit:
            seen.add(ip)
            targets.append(ip)

    rows = _physical_rows()
    if not rows and iface_ip:
        rows = [(iface_ip, "255.255.255.0")]
    if iface_ip:
        rows = ([(iface_ip, "255.255.255.0")]
                + [row for row in rows if row[0] != iface_ip])
    for ip, mask in rows:
        for host in _subnet_hosts(ip, mask, limit - len(targets)):
            add(host)
        add(ip)          # the official client answers on our own address
        if len(targets) >= limit:
            break
    add("127.0.0.1")     # and on loopback
    return targets


async def register_official(ident: dict, device_port: int,
                            timeout: float = 2.0) -> dict | None:
    """Make the official client on this machine point at our port.

    Official Flix answers /ping_v2 by upserting the requester's deviceModal,
    but it ignores an announce for a fingerprint it already stores. A record
    left at 8891 therefore sends our messages back to itself; one ping_v2 is
    what moves it to 18991 (verified against 2.0.4+100). Returns the official
    modal when it answered, otherwise None.
    """
    if device_port == DEVICE_PORT:
        return None
    from .channel import PeerError, ping_v2

    addresses: list[str] = []
    own = default_iface_ip()
    if own:
        addresses.append(own)
    addresses.append("127.0.0.1")
    for ip in addresses:
        try:
            modal = await ping_v2(ip, ident, port=DEVICE_PORT,
                                  device_port=device_port,
                                  timeout=timeout, from_="manual")
        except PeerError:
            continue
        except Exception:  # noqa: BLE001 - registration is best effort
            continue
        fp = modal.get("fingerprint")
        if not fp or fp == ident.get("fingerprint"):
            continue
        modal = dict(modal)
        modal["ip"] = ip
        modal["source"] = ip
        return modal
    return None


async def scan_lan(on_device: Callable[[dict], None], ident: dict,
                   device_port: int = DEVICE_PORT,
                   ports: tuple[int, ...] = (DEVICE_PORT, 18991),
                   hosts: list[str] | None = None,
                   timeout: float = 0.6,
                   concurrency: int = 128) -> list[dict]:
    """Active discovery fallback: probe TCP channel ports with /ping_v2.

    Passive multicast cannot work when UDP 8891 is already owned by another
    process, and official clients only announce at startup. A bounded TCP
    sweep finds both cases and never needs the peer to announce. `ping_v2`
    proves the fingerprint, so a scan hit is as trustworthy as an announce.
    """
    from .channel import ping_v2

    targets = hosts if hosts is not None else local_hosts()
    if not targets:
        return []
    sem = asyncio.Semaphore(concurrency)

    async def probe(ip: str, port: int) -> dict | None:
        async with sem:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(ip, port), timeout)
            except (OSError, asyncio.TimeoutError):
                return None
            with contextlib.suppress(Exception):
                writer.close()
            try:
                modal = await ping_v2(ip, ident, port=port,
                                      device_port=device_port,
                                      timeout=2.0, from_="scan")
            except Exception:
                # 端口通但不应答我们的握手：官方 Flix（任意版本）或别的实现。
                # 以前这里直接丢掉了，所以「只能发现同版本」——把这类设备
                # 标成 unidentified 交给上层，界面照样能看到并尝试官方协议。
                return {
                    "fingerprint": f"ip:{ip}:{port}",
                    "alias": ip,
                    "deviceModel": "unknown",
                    "deviceType": "unknown",
                    "os": "unknown",
                    "port": port,
                    "version": 0,
                    "ip": ip,
                    "source": ip,
                    "unidentified": True,
                    "app": "official-or-other",
                }
            fp = modal.get("fingerprint")
            if not fp or fp == ident.get("fingerprint"):
                return None
            dev = dict(modal)
            dev["ip"] = ip
            dev["source"] = ip
            if not dev.get("port"):
                dev["port"] = port
            return dev

    results = await asyncio.gather(
        *(probe(ip, port) for ip in targets for port in ports))
    seen: set[str] = set()
    found: list[dict] = []
    for dev in results:
        if dev and dev["fingerprint"] not in seen:
            seen.add(dev["fingerprint"])
            found.append(dev)
            with contextlib.suppress(Exception):
                on_device(dev)
    return found
