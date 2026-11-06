"""Shared target resolution for CLI/MCP group + direct sends."""
from __future__ import annotations

import ipaddress

from . import DEVICE_PORT


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def _seen(row: dict) -> int:
    try:
        return int(row.get("seenAt") or 0)
    except (TypeError, ValueError):
        return 0


def resolve_target(store, to: str) -> tuple[str | None, int | None, str]:
    """fingerprint (exact/prefix), or ip[:port] -> (ip, port, fingerprint).

    Falls back to the persisted device table (persistence_devices), mirroring
    how the official client reaches already-running peers (discovery only
    announces at peer startup).

    An address is also mapped back to the stored fingerprint. Returning the
    literal IP as the peer id (the old behaviour) broke every command that
    checks the *local* pair table before sending — ``flix shell 192.168.1.9``
    answered "not-paired" although the board was paired — and stamped
    ``to``/``deviceId`` with an IP where the protocol (and official clients)
    expect a fingerprint. Unknown addresses still send directly, so a peer
    that never announced stays reachable by raw IP.
    """
    port = None
    target = to.strip()
    head, _, tail = target.rpartition(":")
    if tail.isdigit() and head:
        target, port = head, int(tail)
    rows = [row for row in store.devices() if row.get("fingerprint")]
    for row in rows:
        if target and row["fingerprint"].startswith(target) and row.get("ip"):
            return row["ip"], (port or row.get("port")), row["fingerprint"]
    if _is_ip(target):
        same = [row for row in rows if row.get("ip") == target]
        if port is not None:
            exact = [row for row in same
                     if int(row.get("port") or 0) == port]
            if exact:
                same = exact
        if same:
            # DHCP reuse can leave two rows on one address; the device that
            # announced/probed most recently is the one actually answering.
            row = max(same, key=_seen)
            return row["ip"], (port or row.get("port")), row["fingerprint"]
        return target, port, target
    return None, port, to
