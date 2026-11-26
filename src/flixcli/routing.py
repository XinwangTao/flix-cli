"""Shared target resolution for CLI/MCP group + direct sends."""
from __future__ import annotations

from . import DEVICE_PORT, is_port_text


def _prefer_paired(store, rows: list[dict]) -> dict:
    """Pick the row that is actually usable: paired first, then as given."""
    for row in rows:
        if store.is_paired(row["fingerprint"]):
            return row
    return rows[0]


def resolve_target(store, to: str) -> tuple[str | None, int | None, str]:
    """fingerprint (exact/prefix), or ip[:port] -> (ip, port, fingerprint).

    Falls back to the persisted device table (persistence_devices), mirroring
    how the official client reaches already-running peers (discovery only
    announces at peer startup).

    An `ip:port` target must honour the **port**: discovery leaves placeholder
    rows (`ip:192.168.0.9:8891`) next to the real paired device on :18991, and
    matching on the address alone made `flix shell 192.168.0.9:18991` answer
    "not-paired" for a device that is paired on exactly that port.
    """
    port = None
    target = to
    head, _, tail = to.rpartition(":")
    if is_port_text(tail):
        target, port = head, int(tail)
    rows = store.devices()
    # A fingerprint (or prefix) names one device unambiguously.
    for row in rows:
        if row["fingerprint"].startswith(target) and row.get("ip"):
            return row["ip"], (port or row.get("port")), row["fingerprint"]
    if not is_port_text(target.replace(".", "")):
        return None, port, to
    same_ip = [row for row in rows if row.get("ip") == target]
    if port is not None:
        exact = [row for row in same_ip
                 if int(row.get("port") or 0) == int(port)]
        # 同一 IP 上往往同时躺着一堆「发现」留下的占位行（fingerprint 形如
        # ``ip:192.168.0.9:18991``）和真正配对的设备。占位行也占着端口号，
        # 于是 `flix shell <ip>:<那个端口>` 会匹配到占位行、报 not-paired ——
        # 明明这台机器就在同一个 IP 上配对过。所以只有「端口对上且真的配对」
        # 才用它；否则落到下面按 IP 的兜底（并保留请求的端口，
        # `_sibling_device_port` 会再试另一个候选端口）。
        paired_exact = [row for row in exact
                        if store.is_paired(row["fingerprint"])]
        if paired_exact:
            chosen = _prefer_paired(store, paired_exact)
            return chosen["ip"], int(port), chosen["fingerprint"]
    if same_ip:
        chosen = _prefer_paired(store, same_ip)
        return chosen["ip"], (port or chosen.get("port")), chosen["fingerprint"]
    return target, port, to
