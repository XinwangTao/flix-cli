"""Shared target resolution for CLI/MCP group + direct sends."""
from __future__ import annotations

from . import DEVICE_PORT, is_port_text


def resolve_target(store, to: str) -> tuple[str | None, int | None, str]:
    """fingerprint (exact/prefix), or ip[:port] -> (ip, port, fingerprint).

    Falls back to the persisted device table (persistence_devices), mirroring
    how the official client reaches already-running peers (discovery only
    announces at peer startup).
    """
    port = None
    target = to
    head, _, tail = to.rpartition(":")
    if is_port_text(tail):
        target, port = head, int(tail)
    for row in store.devices():
        if row["fingerprint"].startswith(target) and row.get("ip"):
            return row["ip"], (port or row.get("port")), row["fingerprint"]
    # An address that belongs to a known device must resolve to its identity:
    # storing the raw ip:port as the peer hides the message from the device's
    # transcript (the timeline filters by fingerprint).
    rows = [row for row in store.devices() if row.get("ip") == target]
    if rows and port is not None:
        # One address can host two products (official Flix on 8891, RelayDesk
        # on 18991). An explicit port must pick the row that owns it; taking
        # the first row by alias sent the bubble to the wrong fingerprint.
        exact = [row for row in rows
                 if int(row.get("port") or 0) == int(port)]
        if not exact:
            # Nobody known at that port: let the caller prove the identity.
            return target, port, to
        rows = exact
    if rows:
        rows.sort(key=lambda row: int(row.get("seenAt") or 0), reverse=True)
        row = rows[0]
        return row["ip"], (port or row.get("port")), row["fingerprint"]
    if is_port_text(target.replace(".", "")):
        return target, port, to
    return None, port, to
