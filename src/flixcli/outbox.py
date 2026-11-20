"""Durable text delivery: persist first, try once, let the daemon retry.

The transport's job is to hand a message over reliably, so a peer that is
merely offline for a few seconds must not lose it. Every text bubble becomes an
outbox row before the first attempt, and only ``200 received bubble`` counts as
an ack. Non-idempotent actions (shell, upgrade) deliberately stay one-shot.
"""
from __future__ import annotations

import contextlib
import ipaddress
import json

from . import DEVICE_PORT, model
from .channel import PeerError, ping_v2, post_bubble
from .routing import resolve_target


def _acked(reply: str) -> bool:
    return "received bubble" in (reply or "")


def _placeholder(fp: str) -> bool:
    """Scan entries use ``ip:<addr>:<port>`` until a probe proves a fingerprint."""
    return not fp or fp.startswith("ip:")


def _endpoint(text: str) -> bool:
    """True when the target is an address, not an identity."""
    head, _, tail = text.rpartition(":")
    candidate = head if tail.isdigit() and head else text
    try:
        ipaddress.ip_address(candidate)
        return True
    except ValueError:
        return False


def _advertised_port(store, override: int | None) -> int:
    """The device port peers should connect back on.

    A CLI one-shot has no daemon arguments, so it reads what the daemon last
    bound. Sending 8891 while the daemon sits on 18991 would make the official
    client rewrite our record to 8891 — the exact failure this batch fixes.
    """
    if override:
        return int(override)
    raw = (store.prefs() or {}).get("devicePort")
    try:
        return int(raw) if raw else DEVICE_PORT
    except (TypeError, ValueError):
        return DEVICE_PORT


async def _resolve_peer(store, ident: dict, to: str, port: int | None,
                        device_port: int) -> tuple[str | None, int | None, str]:
    """Resolve a target; an unknown ip:port is identified by ping_v2 first.

    Without this, sending to ``192.168.1.9:8891`` put the literal endpoint in
    the bubble's ``to`` field and the official client answered
    ``500 receive bubble failed``. The probe also fills our device table, so
    the next send resolves by fingerprint.
    """
    ip, resolved_port, fp = resolve_target(store, to)
    if not ip:
        return None, None, ""
    use_port = int(resolved_port or port or DEVICE_PORT)
    if not _placeholder(fp) and not (fp == to and _endpoint(to)):
        return ip, use_port, fp
    try:
        modal = await ping_v2(ip, ident, port=use_port,
                              device_port=device_port, timeout=3.0)
    except PeerError:
        return ip, use_port, ""
    real = str(modal.get("fingerprint") or "")
    if not real or real == ident.get("fingerprint"):
        return ip, use_port, ""
    store.upsert_device(real, alias=str(modal.get("alias") or ""),
                        device_model=str(modal.get("deviceModel") or ""),
                        port=modal.get("port"), version=modal.get("version"),
                        ip=ip, os_name=str(modal.get("os") or ""))
    if _placeholder(fp):
        with contextlib.suppress(Exception):
            store.remove_device(fp)
    return ip, use_port, real


async def send_text(store, ident: dict, to: str, text: str,
                    port: int | None = None,
                    device_port: int | None = None) -> dict:
    """Queue the bubble, try once, and report delivered|queued|failed."""
    ip, use_port, fp = await _resolve_peer(
        store, ident, to, port, _advertised_port(store, device_port))
    if not ip:
        return {"ok": False, "error": "no-device",
                "detail": f"unknown target {to!r}; run 'flix discover' first"}
    if not fp:
        return {"ok": False, "error": "unknown-identity",
                "detail": (f"{to} 端口开着，但没有应答 ping_v2；"
                           "这是官方 Flix 或其它版本，先在设置里刷新设备再发送"),
                "to": to}
    bubble = model.text_bubble(ident["fingerprint"], fp, text)
    store.add_outbox(bubble["id"], fp, "text",
                     json.dumps(bubble, ensure_ascii=False))
    result = await _attempt(store, bubble, ip, use_port)
    result["bubble"] = bubble
    return result


async def _attempt(store, bubble: dict, ip: str, port: int) -> dict:
    oid = bubble["id"]
    try:
        reply = await post_bubble(ip, bubble, port=port)
    except PeerError as exc:
        if exc.kind == "rejected":
            # 4xx is a permanent refusal (unpaired, bad params): never retry.
            store.mark_outbox(oid, "failed", f"rejected: {exc.detail}")
            return {"ok": False, "id": oid, "state": "failed",
                    "error": "rejected", "detail": exc.detail, "to": bubble["to"]}
        store.note_outbox_attempt(oid, exc.kind)
        return {"ok": True, "id": oid, "state": "queued", "reason": exc.kind,
                "to": bubble["to"]}
    if not _acked(reply):
        store.note_outbox_attempt(oid, f"bad-ack:{reply!r}")
        return {"ok": True, "id": oid, "state": "queued", "reason": "bad-ack",
                "to": bubble["to"]}
    store.mark_outbox(oid, "delivered")
    return {"ok": True, "id": oid, "state": "delivered", "reply": reply,
            "to": bubble["to"], "ip": ip}


async def flush(store, limit: int = 20) -> dict:
    """Retry queued rows once; called from the daemon every 30 s."""
    store.outbox_expire()
    sent = failed = skipped = 0
    for row in store.outbox_pending(limit):
        ip, port = store.device_ip(row["peer"])
        if not ip:
            skipped += 1
            continue
        try:
            bubble = json.loads(row["payload"])
        except ValueError:
            store.mark_outbox(row["id"], "failed", "bad-payload")
            failed += 1
            continue
        result = await _attempt(store, bubble, ip, port or DEVICE_PORT)
        if result.get("state") == "delivered":
            sent += 1
        elif result.get("state") == "failed":
            failed += 1
    return {"sent": sent, "failed": failed, "skipped": skipped,
            "pending": len(store.outbox_pending(1000))}
