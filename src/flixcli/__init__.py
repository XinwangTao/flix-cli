"""flixcli package: interoperable reimplementation of the Flix LAN protocol."""

import ipaddress
import json
import os
import socket
import struct
import sys
import time

try:  # single source of truth: the installed package metadata
    from importlib.metadata import PackageNotFoundError, version as _dist_version
    try:
        __version__ = _dist_version("flixcli")
    except PackageNotFoundError:  # running from a source tree without install
        __version__ = "0.0.0+source"
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

def build_id() -> str:
    """Opaque marker for *this* build, stable across processes.

    A version string is not enough to decide whether a running daemon came
    from the current binary: both a pre-fix and a post-fix build can call
    themselves 2.4.0. The GUI compares this marker before reusing a daemon.
    """
    target = sys.executable if getattr(sys, "frozen", False) else __file__
    try:
        st = os.stat(target)
    except OSError:
        return "unknown"
    return f"{int(st.st_mtime)}:{st.st_size}"


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


# FriendlyName/Description fragments of adapters that never carry a peer.
# On the reporting host VMware's VMnet1 (192.168.73.1/24, no gateway) took over
# the multicast route every time Wi-Fi blinked: announces sent "through" it
# reach no phone, so this PC silently vanished from the phone's device list and
# pairing started from that list could never work. Hyper-V, Wi-Fi Direct,
# Bluetooth PAN, Wintun/Sangfor VPN VNICs and Clash-style tunnels fail the same
# way - and some, like the Sangfor VNIC on 10.10.200.68/24, even look like a
# normal private LAN.
_VIRTUAL_ADAPTER_MARKERS = (
    "virtual", "vmware", "vbox", "hyper-v", "vethernet", "default switch",
    "wi-fi direct", "loopback", "bluetooth", "wintun", "tunnel", "vpn",
    "tap-windows", "tap adapter", "npcap", "wsl", "docker", "tailscale",
    "zerotier", "wireguard", "spacedesk", "vnic", "sangfor", "anyconnect",
    "openvpn", "clash", "radmin",
)

# Linux/macOS device names: same idea, matched by the kernel interface name.
_UNIX_VIRTUAL_PREFIXES = (
    "docker", "veth", "virbr", "br-", "vmnet", "tun", "tap", "wg", "zt",
    "tailscale", "utun", "bridge", "awdl", "llw", "clash", "meta", "vboxnet",
)

_ADAPTER_CACHE: tuple[float, dict[int, str]] = (0.0, {})


def _adapter_is_virtual(name: str) -> bool:
    text = (name or "").strip().lower()
    if not text:
        return False
    return any(marker in text for marker in _VIRTUAL_ADAPTER_MARKERS)


def _win_adapter_names() -> dict[int, str]:
    """{ifIndex: "FriendlyName Description"} via GetAdaptersAddresses (60s cache).

    refresh_iface() polls the interface list every few seconds and the Win32
    call allocates a buffer each time, so the answer is cached briefly.
    """
    global _ADAPTER_CACHE
    now = time.monotonic()
    cached_at, cached = _ADAPTER_CACHE
    if cached and now - cached_at < 60:
        return cached
    names: dict[int, str] = {}
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class _Adapter(ctypes.Structure):
                pass

            # Layout up to FriendlyName (Windows SDK IP_ADAPTER_ADDRESSES); the
            # tail (physical address, mtu, ...) is never dereferenced.
            _Adapter._fields_ = [
                ("Length", wintypes.ULONG),
                ("IfIndex", wintypes.DWORD),
                ("Next", ctypes.POINTER(_Adapter)),
                ("AdapterName", ctypes.c_char_p),
                ("FirstUnicastAddress", ctypes.c_void_p),
                ("FirstAnycastAddress", ctypes.c_void_p),
                ("FirstMulticastAddress", ctypes.c_void_p),
                ("FirstDnsServerAddress", ctypes.c_void_p),
                ("DnsSuffix", ctypes.c_wchar_p),
                ("Description", ctypes.c_wchar_p),
                ("FriendlyName", ctypes.c_wchar_p),
            ]

            size = wintypes.ULONG(15000)
            for _ in range(3):
                buf = ctypes.create_string_buffer(size.value)
                rc = ctypes.windll.iphlpapi.GetAdaptersAddresses(
                    wintypes.ULONG(socket.AF_INET), 0, None, buf,
                    ctypes.byref(size))
                if rc == 111:  # ERROR_BUFFER_OVERFLOW: retry with size it asks
                    continue
                if rc != 0:
                    break
                node = ctypes.cast(buf, ctypes.POINTER(_Adapter))
                while node:
                    row = node.contents
                    names[int(row.IfIndex)] = " ".join(
                        part for part in (row.FriendlyName, row.Description)
                        if part)
                    node = row.Next
                break
        except Exception:  # noqa: BLE001 - best effort, names are a nicety
            names = {}
    _ADAPTER_CACHE = (now, names)
    return names


def _win_virtual_ifindexes() -> set[int]:
    return {idx for idx, name in _win_adapter_names().items()
            if _adapter_is_virtual(name)}


def _is_unix_virtual_name(name: str) -> bool:
    text = (name or "").strip().lower()
    return bool(text) and text.startswith(_UNIX_VIRTUAL_PREFIXES)


def _iface_name_for_ip(ip: str) -> str:
    """Interface name holding ``ip`` on Linux (best effort)."""
    if not sys.platform.startswith("linux"):
        return ""
    try:
        import fcntl
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return ""
    try:
        for _idx, name in socket.if_nameindex():
            req = struct.pack("256s", name.encode()[:15])
            try:
                addr = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, req)[20:24])
            except OSError:
                continue
            if addr == ip:
                return name
    finally:
        s.close()
    return ""


def virtual_interfaces() -> list[tuple[str, str]]:
    """[(address, adapter name)] for adapters that must not carry an announce.

    Exposed so `flix netcheck` can explain a surprising interface choice on a
    multi-NIC host instead of leaving the user staring at a VPN address.
    """
    rows = _win_ipv4_rows()
    if rows:
        names = _win_adapter_names()
        return [(ip, names.get(idx, "")) for ip, _mask, idx in rows
                if _adapter_is_virtual(names.get(idx, ""))]
    out: list[tuple[str, str]] = []
    for ip, _mask in _linux_ipv4_table():
        name = _iface_name_for_ip(ip)
        if _is_unix_virtual_name(name):
            out.append((ip, name))
    return out


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

    Virtual adapters (VMware/Hyper-V/VPN/Wi-Fi Direct) are dropped: nobody
    answers an announce on them, and joining there only costs a membership
    plus a datagram that never reaches a phone.
    """
    rows: list[tuple[str, str | None]] = list(_win_ipv4_table())
    if not rows:
        rows = list(_linux_ipv4_table())
    if not rows:
        routed = _routed_ip()
        if routed:
            rows = [(routed, None)]
    virtual_ips = {ip for ip, _name in virtual_interfaces()}
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for ip, mask in rows:
        if ip in virtual_ips:
            continue
        if _usable(ip, mask) and ip not in seen:
            seen.add(ip)
            out.append((ip, mask or ""))
    return out


def _pinned_iface_pref() -> str:
    """Operator pin from shared_preferences.json ("multicast_if").

    The desktop GUI starts its daemon without command-line flags, so a pref is
    the only way to keep a hand-picked interface across restarts.
    """
    try:
        from .config import PREFS_PATH
        data = json.loads(PREFS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - missing/unreadable prefs are normal
        return ""
    value = data.get("multicast_if") if isinstance(data, dict) else None
    return value.strip() if isinstance(value, str) else ""


_last_iface = ""


def default_iface_ip() -> str:
    """LAN IPv4 to use for multicast join + send, best effort.

    ``set_local_ip_override`` / FLIX_MULTICAST_IF / the "multicast_if" pref
    win. Otherwise prefer an interface that owns a real default route, skipping
    virtual adapters, and stay there: re-picking on every poll is what let a
    Wi-Fi blink hand announce duty to VMware's VMnet1.
    """
    global _last_iface
    if _local_ip_override:
        return _local_ip_override
    env = os.environ.get("FLIX_MULTICAST_IF", "").strip()
    if env:
        return env
    pinned = _pinned_iface_pref()
    if pinned:
        return pinned
    rows = dict(_win_ipv4_table())
    virtual_ips = {ip for ip, _name in virtual_interfaces()}
    candidates = [ip for ip, mask in rows.items()
                  if ip not in virtual_ips and _usable(ip, mask)]
    # Hysteresis: keep the interface we already announced on for as long as it
    # still exists, so a momentary default-route change does not move our
    # announces onto a link no peer shares.
    if _last_iface in candidates:
        return _last_iface
    # Prefer an interface that owns a real default route: on multi-NIC hosts
    # the first "10." address may sit on an isolated switch with no gateway.
    gateways = _gateway_ifindexes()
    gateway_ips = [ip for ip, mask, idx in _win_ipv4_rows()
                   if idx in gateways and ip in candidates]
    routed = _routed_ip()
    pick = ""
    for pool in (gateway_ips, [routed], candidates):
        for prefix in ("192.168.", "10.", "172."):
            for ip in pool:
                if ip and ip.startswith(prefix):
                    pick = ip
                    break
            if pick:
                break
        if pick:
            break
    if not pick:
        pick = candidates[0] if candidates else (routed if _usable(routed, None) else "")
    _last_iface = pick
    return pick


def local_ip() -> str:
    """Best-effort LAN IP (see default_iface_ip)."""
    return default_iface_ip()
