"""flixcli package: interoperable reimplementation of the Flix LAN protocol."""

import os
import socket
import struct
import sys

__version__ = "0.1.0"

MULTICAST_GROUP = "224.0.0.168"      # [IDA pending: DiscoverManager] / [RT capture]
DEVICE_PORT = 8891                    # TCP+UDP device channel
WEB_PORT_DEFAULT = 8892
DESKTOP_API_PORT = 8898
ANNOUNCE_BURST = 11                   # [RT capture_discovery.txt]


def _win_ipv4_table() -> list[tuple[str, str]]:
    """[(address, netmask)] from iphlpapi GetIpAddrTable (empty off-Windows)."""
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
                 socket.inet_ntoa(struct.pack("<I", rows[i].dwMask)))
                for i in range(count)]
    except Exception:  # noqa: BLE001 - best effort, fall back to the route trick
        return []


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
    if not ip or ip.startswith(("0.", "127.", "169.254.")):
        return False
    # /32 host routes are VPN/virtual gateway adapters (e.g. vgate0); they win
    # the multicast route on some hosts even though no peer shares that link.
    return mask is None or mask != "255.255.255.255"


def default_iface_ip() -> str:
    """LAN IPv4 to use for multicast join + send, best effort.

    FLIX_MULTICAST_IF overrides. Otherwise prefer the interface the OS routes
    through, but reject it when iphlpapi reports a /32 mask, then fall back to
    the first private address with a real subnet mask.
    """
    env = os.environ.get("FLIX_MULTICAST_IF", "").strip()
    if env:
        return env
    rows = dict(_win_ipv4_table())
    routed = _routed_ip()
    if _usable(routed, rows.get(routed)):
        return routed
    candidates = [ip for ip, mask in rows.items() if _usable(ip, mask)]
    for prefix in ("192.168.", "10.", "172."):
        for ip in candidates:
            if ip.startswith(prefix):
                return ip
    return candidates[0] if candidates else routed


def local_ip() -> str:
    """Best-effort LAN IP (see default_iface_ip)."""
    return default_iface_ip()
