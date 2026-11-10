"""flixcli package: interoperable reimplementation of the Flix LAN protocol."""

import ipaddress
import os
import socket
import struct
import sys

try:  # single source of truth: the installed package metadata
    from importlib.metadata import PackageNotFoundError, version as _dist_version
    try:
        __version__ = _dist_version("flixcli")
    except PackageNotFoundError:  # running from a source tree without install
        __version__ = "2.5.2"
except ImportError:  # pragma: no cover - Python < 3.8
    __version__ = "0.0.0+source"

# A checkout that was never `pip install`ed still has to report the version it
# ships as: `flix --version`, `flix self` and MCP serverInfo surface this, and
# tests/test_version.py holds it to pyproject.toml.
if __version__ in ("0.0.0+source", "0.1.0"):
    try:
        import tomllib as _toml
        from pathlib import Path as _Path
        _pyproject = _Path(__file__).resolve().parents[2] / "pyproject.toml"
        __version__ = str(_toml.loads(
            _pyproject.read_text(encoding="utf-8"))["project"]["version"])
    except Exception:  # pragma: no cover - best effort only
        pass

MULTICAST_GROUP = "224.0.0.168"      # [IDA pending: DiscoverManager] / [RT capture]
DEVICE_PORT = 8891                    # TCP+UDP device channel
WEB_PORT_DEFAULT = 8892
DESKTOP_API_PORT = 8898
ANNOUNCE_BURST = 11                   # [RT capture_discovery.txt]

# Address space that can carry a LAN announce.  VPN/tunnel adapters that serve
# this space are not LAN links: Clash TUN hands out 198.18.0.1/30 (benchmark
# range), Tailscale/CGNAT uses 100.64/10, and APIPA/link-local never has peers.
_VIRTUAL_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "198.18.0.0/15",   # RFC 2544 benchmarking / Clash TUN, Surge, sing-box
    "100.64.0.0/10",   # RFC 6598 CGNAT / Tailscale
    "169.254.0.0/16",  # APIPA
    "0.0.0.0/8",
    "127.0.0.0/8",
    "224.0.0.0/4",
    "240.0.0.0/4",
))

_local_ip_override = ""


def set_local_ip_override(ip: str) -> None:
    """Pin the advertised local IP (daemon ``--multicast-if`` / netcheck)."""
    global _local_ip_override
    _local_ip_override = ip.strip() if isinstance(ip, str) else ""


def is_port_text(text: str) -> bool:
    """True only for a non-empty ASCII run of digits.

    str.isdigit() also accepts superscripts and other Unicode numerics
    (``'²'.isdigit()`` is True) that ``int()`` then rejects, so it must never
    guard a port conversion.
    """
    return isinstance(text, str) and bool(text) and text.isascii() and text.isdigit()


def is_virtual_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return addr.is_loopback or addr.is_link_local or any(
        addr in net for net in _VIRTUAL_NETWORKS)



def _win_ipv4_rows() -> list[tuple[str, str, int]]:
    """[(address, netmask, ifIndex)] from iphlpapi (empty off-Windows)."""
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        class _Row(ctypes.Structure):
            _fields_ = [("dwAddr", wintypes.DWORD), ("dwIndex", wintypes.DWORD),
                        ("dwMask", wintypes.DWORD), ("dwBCastAddr", wintypes.DWORD),
                        ("dwReasmSize", wintypes.DWORD),
                        ("unused1", wintypes.USHORT), ("wType", wintypes.USHORT)]

        class _Table(ctypes.Structure):
            _fields_ = [("dwNumEntries", wintypes.DWORD), ("table", _Row * 1)]

        fn = ctypes.windll.iphlpapi.GetIpAddrTable
        size = wintypes.ULONG(0)
        if fn(None, ctypes.byref(size), False) not in (0, 122):  # INSUFFICIENT_BUFFER
            return []
        buf = ctypes.create_string_buffer(size.value)
        if fn(buf, ctypes.byref(size), False) != 0:
            return []
        # The row count is a runtime value; cast the payload as a flat _Row
        # pointer instead of a fixed-size array (multi-address hosts would
        # otherwise raise IndexError past the declared _Row * 1 field).
        count = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD)).contents.value
        rows = ctypes.cast(ctypes.byref(buf, 4), ctypes.POINTER(_Row))
        return [(socket.inet_ntoa(struct.pack("<I", rows[i].dwAddr)),
                 socket.inet_ntoa(struct.pack("<I", rows[i].dwMask)),
                 int(rows[i].dwIndex))
                for i in range(count)]
    except Exception:  # noqa: BLE001 - best effort, fall back to the route trick
        return []


def _win_ipv4_table() -> list[tuple[str, str]]:
    """[(address, netmask)] from iphlpapi GetIpAddrTable (empty off-Windows)."""
    return [(ip, mask) for ip, mask, _idx in _win_ipv4_rows()]


def _gateway_ifindexes() -> set[int]:
    """Interfaces that own a real default route (nextHop != 0.0.0.0).

    On a multi-NIC Windows box the first "10." address is not necessarily the
    LAN: Ethernet 3 here is 10.10.200.41 with no gateway, while the usable LAN
    is 10.37.73.198/24 behind 10.37.73.254. Pick the one that can actually
    route off-link. Point-to-point TUN routes usually carry nextHop 0 and are
    excluded (they are also filtered by _usable).
    """
    if sys.platform != "win32":
        return set()
    try:
        import ctypes
        from ctypes import wintypes

        class _Fwd(ctypes.Structure):
            _fields_ = [
                ("dwForwardDest", wintypes.DWORD),
                ("dwForwardMask", wintypes.DWORD),
                ("dwForwardPolicy", wintypes.DWORD),
                ("dwForwardNextHop", wintypes.DWORD),
                ("dwForwardIfIndex", wintypes.DWORD),
                ("dwForwardType", wintypes.DWORD),
                ("dwForwardProto", wintypes.DWORD),
                ("dwForwardAge", wintypes.DWORD),
                ("dwForwardNextHopAS", wintypes.DWORD),
                ("dwForwardMetric1", wintypes.DWORD),
                ("dwForwardMetric2", wintypes.DWORD),
                ("dwForwardMetric3", wintypes.DWORD),
                ("dwForwardMetric4", wintypes.DWORD),
                ("dwForwardMetric5", wintypes.DWORD),
            ]

        fn = ctypes.windll.iphlpapi.GetIpForwardTable
        size = wintypes.ULONG(0)
        if fn(None, ctypes.byref(size), False) not in (0, 122):
            return set()
        buf = ctypes.create_string_buffer(size.value)
        if fn(buf, ctypes.byref(size), False) != 0:
            return set()
        count = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD)).contents.value
        rows = ctypes.cast(ctypes.byref(buf, 4), ctypes.POINTER(_Fwd))
        return {int(rows[i].dwForwardIfIndex) for i in range(count)
                if rows[i].dwForwardDest == 0
                and rows[i].dwForwardNextHop != 0}
    except Exception:  # noqa: BLE001
        return set()


def _routed_ip() -> str:
    """Source IP the OS would pick for outbound traffic (no packets are sent)."""
    for target in (("8.8.8.8", 53), (MULTICAST_GROUP, DEVICE_PORT)):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(target)
            return s.getsockname()[0]
        except OSError:
            continue
        finally:
            s.close()
    return ""


def local_ipv4s() -> list[str]:
    """Every non-loopback IPv4 address of this host (best effort).

    Used to keep our own machine out of device lists: an older install can
    announce itself under a different fingerprint, so matching on fingerprint
    alone is not enough.
    """
    raw: list[str] = [ip for ip, _mask in _win_ipv4_table()]
    if not raw:
        try:
            raw = [info[4][0] for info in
                   socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
        except OSError:
            raw = []
    routed = _routed_ip()
    if routed:
        raw.append(routed)
    seen: set[str] = set()
    out: list[str] = []
    for ip in raw:
        if ip and ip != "0.0.0.0" and not ip.startswith("127.") and ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out


def _usable(ip: str, mask: str | None) -> bool:
    if not ip:
        return False
    # Reject VPN/TUN/CGNAT space outright: Clash TUN's 198.18.0.1/30 wins the
    # default route on this host yet has no LAN peers, so every multicast
    # datagram sent "through" it is dropped.
    if is_virtual_ip(ip):
        return False
    # /32 (and /31) host routes are VPN/virtual gateway adapters (e.g. vgate0);
    # they win the multicast route even though no peer shares that link.
    if mask in ("255.255.255.255", "255.255.255.254"):
        return False
    return True


def _linux_ipv4_table() -> list[tuple[str, str]]:
    """[(address, netmask)] from SIOCGIFADDR/SIOCGIFNETMASK (empty off-Linux)."""
    if not sys.platform.startswith("linux"):
        return []
    try:
        import fcntl
    except ImportError:
        return []
    out: list[tuple[str, str]] = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _idx, name in socket.if_nameindex():
            req = struct.pack("256s", name.encode()[:15])
            try:
                addr = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, req)[20:24])
                mask = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x891b, req)[20:24])
            except OSError:
                continue
            out.append((addr, mask))
    except OSError:
        return out
    finally:
        s.close()
    return out


def local_ipv4_table() -> list[tuple[str, str]]:
    """[(address, netmask)] for LAN-capable interfaces, best effort.

    Announce/join has to cover every interface a peer might share with us:
    a host on Wi-Fi plus a wired board on ICS only ever sees the board if the
    Ethernet link is included, and the default route alone would skip it.
    """
    rows: list[tuple[str, str | None]] = list(_win_ipv4_table())
    if not rows:
        rows = list(_linux_ipv4_table())
    if not rows:
        routed = _routed_ip()
        if routed:
            rows = [(routed, None)]
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for ip, mask in rows:
        if _usable(ip, mask) and ip not in seen:
            seen.add(ip)
            out.append((ip, mask or ""))
    return out

def default_iface_ip() -> str:
    """LAN IPv4 to use for multicast join + send, best effort.

    ``set_local_ip_override`` / FLIX_MULTICAST_IF win. Otherwise prefer the
    interface the OS routes through, rejecting virtual/VPN space and host
    routes, then fall back to the first real private address.
    """
    if _local_ip_override:
        return _local_ip_override
    env = os.environ.get("FLIX_MULTICAST_IF", "").strip()
    if env:
        return env
    rows = dict(_win_ipv4_table())
    routed = _routed_ip()
    if _usable(routed, rows.get(routed)):
        return routed
    candidates = [ip for ip, mask in rows.items() if _usable(ip, mask)]
    # Prefer an interface that owns a real default route: on multi-NIC hosts
    # the first "10." address may sit on an isolated switch with no gateway.
    gateways = _gateway_ifindexes()
    gateway_ips = [ip for ip, mask, idx in _win_ipv4_rows()
                   if idx in gateways and _usable(ip, mask)]
    for pool in (gateway_ips, candidates):
        for prefix in ("192.168.", "10.", "172."):
            for ip in pool:
                if ip.startswith(prefix):
                    return ip
    return candidates[0] if candidates else routed


def local_ip() -> str:
    """Best-effort LAN IP (see default_iface_ip)."""
    return default_iface_ip()
