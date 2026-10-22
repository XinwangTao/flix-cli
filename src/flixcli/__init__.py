"""flixcli package: interoperable reimplementation of the Flix LAN protocol."""

import socket

__version__ = "0.1.0"

MULTICAST_GROUP = "224.0.0.168"      # [IDA pending: DiscoverManager] / [RT capture]
DEVICE_PORT = 8891                    # TCP+UDP device channel
WEB_PORT_DEFAULT = 8892
DESKTOP_API_PORT = 8898
ANNOUNCE_BURST = 11                   # [RT capture_discovery.txt]


def local_ip() -> str:
    """Best-effort LAN IP (UDP connect trick; no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((MULTICAST_GROUP, DEVICE_PORT))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()
