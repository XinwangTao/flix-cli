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
import json
import socket
import sys
from typing import Callable

from . import ANNOUNCE_BURST, DEVICE_PORT, MULTICAST_GROUP, default_iface_ip


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

    def announce(self, ident: dict, times: int = ANNOUNCE_BURST,
                 port: int = DEVICE_PORT) -> None:
        payload = build_announce(ident, port=port)
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 8)
            if self.iface:
                tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                              socket.inet_aton(self.iface))
            for _ in range(times):
                tx.sendto(payload, (MULTICAST_GROUP, DEVICE_PORT))
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
