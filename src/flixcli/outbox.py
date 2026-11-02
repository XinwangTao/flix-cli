"""Durable text delivery: persist first, try once, let the daemon retry.

The transport's job is to hand a message over reliably, so a peer that is
merely offline for a few seconds must not lose it. Every text bubble becomes an
outbox row before the first attempt, and only ``200 received bubble`` counts as
an ack. Non-idempotent actions (shell, upgrade) deliberately stay one-shot.
"""
from __future__ import annotations

import json

from . import DEVICE_PORT, model
from .channel import PeerError, post_bubble
from .routing import resolve_target


def _acked(reply: str) -> bool:
    return "received bubble" in (reply or "")


async def send_text(store, ident: dict, to: str, text: str,
                    port: int | None = None) -> dict:
    """Queue the bubble, try once, and report delivered|queued|failed."""
    ip, resolved_port, fp = resolve_target(store, to)
    if not ip:
        return {"ok": False, "error": "no-device",
                "detail": f"unknown target {to!r}; run 'flix discover' first"}
    bubble = model.text_bubble(ident["fingerprint"], fp, text)
    store.add_outbox(bubble["id"], fp, "text",
                     json.dumps(bubble, ensure_ascii=False))
    result = await _attempt(store, bubble, ip, resolved_port or port or DEVICE_PORT)
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
