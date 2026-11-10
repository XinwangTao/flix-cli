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
import json
import socket
import struct
import sys
import time
from typing import Callable

from . import (ANNOUNCE_BURST, DEVICE_PORT, MULTICAST_GROUP, local_ipv4_table,
               default_iface_ip)
from .presence import PRESENCE_TTL_MS


_TEXT_FIELD_LIMITS = {"alias": 128, "deviceModel": 64, "os": 32}
_MAX_FINGERPRINT_CHARS = 128


def _bounded_ident(ident: dict, key: str, default: str) -> str:
    value = ident.get(key, default)
    if value is None:
        value = default
    return (value if isinstance(value, str) else str(value))[
        :_TEXT_FIELD_LIMITS[key]]


def build_announce(ident: dict, port: int = DEVICE_PORT, app_version: int = 1) -> bytes:
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = DEVICE_PORT
    if not 1 <= port <= 65535:
        port = DEVICE_PORT
    inner = {
        "deviceModal": {
            "alias": _bounded_ident(ident, "alias", ""),
            "deviceModel": _bounded_ident(ident, "deviceModel", "Windows"),
            "deviceType": ident.get("deviceType", "desktop"),
            "fingerprint": ident.get("fingerprint", ""),
            "port": port,
            "version": app_version,
            "ip": "",
            "host": "",
            "from": "",
            "os": _bounded_ident(ident, "os", "windows"),
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
        if not isinstance(modal, dict):
            return None
        fingerprint = modal.get("fingerprint")
        if not isinstance(fingerprint, str):
            return None
        fingerprint = fingerprint.strip()
        if not fingerprint or len(fingerprint) > _MAX_FINGERPRINT_CHARS:
            return None
        dev = dict(modal)
        dev["fingerprint"] = fingerprint
        # Bound peer-controlled display strings: an alias close to the UDP
        # limit would otherwise bloat sqlite, logs and every GUI/web refresh.
        for key, limit in _TEXT_FIELD_LIMITS.items():
            if key in dev:
                value = dev[key]
                if value is None:
                    dev[key] = ""
                else:
                    dev[key] = (value if isinstance(value, str)
                                else str(value))[:limit]
        # A bad port must not reach the device table or routing: non-numeric,
        # zero and out-of-range values all mean "use the protocol default".
        raw_port = dev.get("port")
        if isinstance(raw_port, bool):
            port = DEVICE_PORT
        else:
            try:
                port = int(raw_port or DEVICE_PORT)
            except (TypeError, ValueError):
                port = DEVICE_PORT
        if not 1 <= port <= 65535:
            port = DEVICE_PORT
        dev["port"] = port
        if not dev.get("ip"):
            dev["ip"] = src_ip
        dev["source"] = src_ip
        return dev
    except (ValueError, UnicodeDecodeError, TypeError):
        return None


class EndpointTracker:
    """Remember every TCP endpoint a fingerprint announced from.

    One machine can run more than one flixd with the same identity: the default
    daemon on 8891 plus ``RelayDesk.exe serve --device-port 18991`` (the
    documented coexistence layout), or a debug instance.  Both re-announce
    every 30 s, so "last announce wins" rewrote ``persistence_devices.port``
    twice a minute.  Routing, outbox, clipboard sync and the file-confirm ack
    then raced between the two ports -- harmless while both processes live,
    but messages to a stopped instance failed until the survivor's next
    announce.  It also spammed ``[discover]`` and ``devicesChanged`` for what
    is really one unchanged device.

    Keep every (ip, port) alive for the presence TTL and pick the lowest port
    on the announced IP.  8891 is the protocol default, so a coexisting
    daemon on 18991 never steals traffic.  Selection only moves after the
    chosen endpoint has been silent for a whole TTL (e.g. the default daemon
    was stopped, leaving only 18991), or when a lower port comes back.
    """

    def __init__(self, ttl_ms: int = PRESENCE_TTL_MS):
        self.ttl_ms = int(ttl_ms)
        self._seen: dict[str, dict[tuple[str, int], int]] = {}

    def remember(self, fingerprint: str, ip: str, port: int,
                 now_ms: int | None = None) -> None:
        if not fingerprint or not ip or not port:
            return
        now = _now_ms(now_ms)
        rows = self._seen.setdefault(fingerprint, {})
        rows[(ip, int(port))] = now
        cutoff = now - self.ttl_ms
        for key, seen in list(rows.items()):
            if seen < cutoff:
                del rows[key]

    def select(self, fingerprint: str, ip: str,
               now_ms: int | None = None) -> tuple[str, int] | None:
        rows = self._seen.get(fingerprint) or {}
        if not rows:
            return None
        now = _now_ms(now_ms)
        cutoff = now - self.ttl_ms
        live = [(ep_ip, ep_port) for (ep_ip, ep_port), seen in rows.items()
                if ep_ip == ip and seen >= cutoff]
        if not live:
            return None
        return min(live, key=lambda ep: ep[1])

    def forget(self, fingerprint: str) -> None:
        self._seen.pop(fingerprint, None)


def _now_ms(now_ms: int | None = None) -> int:
    if now_ms is not None:
        return int(now_ms)
    return int(time.time() * 1000)


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
        # Every LAN-capable address, not just the default route: a host can sit
        # on Wi-Fi and a wired subnet at once, and peers on the second one only
        # hear us if we join and announce there too.
        self._pinned = iface_ip is not None
        self.ifaces: list[str] = ([iface_ip] if iface_ip
                                  else [ip for ip, _mask in local_ipv4_table()])
        self.iface = iface_ip or (self.ifaces[0] if self.ifaces
                                  else (default_iface_ip() or None))
        if self.iface and self.iface not in self.ifaces:
            self.ifaces.insert(0, self.iface)
        self.transport: asyncio.DatagramTransport | None = None
        self.bound = False

    def _open(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", DEVICE_PORT))
        joined = 0
        for ip in (self.ifaces or ["0.0.0.0"]):
            mreq = (socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton(ip))
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                joined += 1
            except OSError:
                continue
        if not joined:
            raise OSError("no interface accepted the multicast join")
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
        """Subnet-directed broadcast for each joined interface, then the
        limited broadcast as a catch-all."""
        addrs: list[str] = []
        rows = local_ipv4_table()
        for ip in self.ifaces:
            mask = next((m for a, m in rows if a == ip), "")
            if not mask or mask == "255.255.255.255":
                continue
            try:
                n = struct.unpack(">I", socket.inet_aton(ip))[0]
                m = struct.unpack(">I", socket.inet_aton(mask))[0]
                bc = socket.inet_ntoa(struct.pack(">I", n | (~m & 0xFFFFFFFF)))
            except OSError:
                continue
            if bc not in addrs:
                addrs.append(bc)
        addrs.append("255.255.255.255")
        return addrs

    async def refresh_iface(self) -> bool:
        """Re-detect the outbound interface and rebind if it moved.

        A board that hops between eth0 and Wi-Fi (DHCP renew, cable replug,
        hotspot fallback) keeps its old multicast membership otherwise: peers
        still reach us over TCP but never see our announces again.
        """
        want_list = [ip for ip, _mask in local_ipv4_table()]
        want = default_iface_ip() or None
        if self._pinned:
            moved = want != self.iface
        else:
            moved = want != self.iface or (bool(want_list) and want_list != self.ifaces)
        if not moved or not self.bound:
            return False
        old = self.iface
        self.iface = want
        self.ifaces = ([want] if self._pinned
                       else (want_list or ([want] if want else [])))
        try:
            if self.transport is not None:
                self.transport.close()
        except Exception:
            pass
        self.transport = None
        self.bound = False
        ok = await self.start()
        print(f"[discovery] interface {old or 'default'} -> "
              f"{self.iface or 'default'}{'' if ok else ' (rebind failed)'}",
              file=sys.stderr, flush=True)
        return ok

    def announce(self, ident: dict, times: int = ANNOUNCE_BURST,
                 port: int = DEVICE_PORT) -> None:
        payload = build_announce(ident, port=port)
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 8)
            tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            # Wire dst port is DEVICE_PORT [RT capture_discovery.txt]: every
            # listener (official + flixcli) binds 8891 for discovery regardless
            # of its TCP channel port; `port` only labels the payload modal.
            targets = [(bc, DEVICE_PORT) for bc in self._broadcast_addrs()]
            for _ in range(times):
                for ip in (self.ifaces or [None]):
                    if ip:
                        with contextlib.suppress(OSError):
                            tx.setsockopt(socket.IPPROTO_IP,
                                          socket.IP_MULTICAST_IF,
                                          socket.inet_aton(ip))
                    with contextlib.suppress(OSError):
                        tx.sendto(payload, (MULTICAST_GROUP, DEVICE_PORT))
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
