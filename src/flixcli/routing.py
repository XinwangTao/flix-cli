"""Shared target resolution for CLI/MCP group + direct sends."""
from __future__ import annotations

from . import DEVICE_PORT


def resolve_target(store, to: str) -> tuple[str | None, int | None, str]:
    """fingerprint (exact/prefix), or ip[:port] -> (ip, port, fingerprint).

    Falls back to the persisted device table (persistence_devices), mirroring
    how the official client reaches already-running peers (discovery only
    announces at peer startup).
    """
    port = None
    target = to
    head, _, tail = to.rpartition(":")
    if tail.isdigit():
        target, port = head, int(tail)
    for row in store.devices():
        if row["fingerprint"].startswith(target) and row.get("ip"):
            return row["ip"], (port or row.get("port")), row["fingerprint"]
    if target.replace(".", "").isdigit():
        return target, port, to
    return None, port, to
