"""Device presence policy.

A peer is "online" only while its last contact (announce or probe) is inside
PRESENCE_TTL_MS. Stored devices keep their IP forever, so IP presence alone
would paint stale entries green after a restart.
"""
from __future__ import annotations

import time

PRESENCE_TTL_MS = 90_000


def is_online(device: dict, now_ms: int | None = None) -> bool:
    """True when the device has an IP and a seenAt inside the TTL window."""
    if not device or not device.get("ip"):
        return False
    try:
        seen = int(device.get("seenAt") or 0)
    except (TypeError, ValueError):
        return False
    if seen <= 0:
        return False
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return now - seen <= PRESENCE_TTL_MS
