"""Agent-to-agent channel (v2K, docs/archify/relaydesk_agent_bridge.html).

Two machines talk AI-to-AI over the existing paired channel. The transport is
deliberately dumb: it moves text envelopes and stores them. Nothing arriving
here is executed — the receiving AI decides what (if anything) to do.

Flow: send -> queue -> /intent rdAgent -> trust gate -> store -> inbox.
"""
from __future__ import annotations

import asyncio
import time
import uuid

from . import DEVICE_PORT
from .channel import PeerError, post_intent
from .model import agent_intent
from .routing import resolve_target

MAX_BODY = 8000
# Retry forever while the peer is merely offline; give up only on envelopes that
# have been sitting for a week (the row stays, so nothing is silently lost).
RETRY_WINDOW_MS = 7 * 24 * 3600 * 1000


def _clean(text: str) -> str:
    body = (text or "").strip()
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY]
    return body


async def send_agent_message(store, ident: dict, target: str, text: str,
                             reply_to: str = "") -> dict:
    """Queue an envelope for `target`, then try to deliver it once.

    Returns immediately with state delivered|queued: waiting on the peer's AI
    would block the caller's turn, and the outbox owns the retries.
    """
    body = _clean(text)
    if not body:
        return {"ok": False, "error": "empty-body"}
    ip, port, fp = resolve_target(store, target)
    if not ip:
        return {"ok": False, "error": "unknown-target", "target": target}
    mid = str(uuid.uuid4())
    created = int(time.time() * 1000)
    store.add_agent_message(mid, "out", fp, body, reply_to, created=created)
    intent = agent_intent(ident.get("fingerprint", ""), fp, mid, body,
                          reply_to, created)
    try:
        await post_intent(ip, intent, port=port or DEVICE_PORT, timeout=5)
    except PeerError as exc:
        store.note_agent_attempt(mid)
        return {"ok": True, "id": mid, "state": "queued", "to": fp,
                "reason": exc.kind}
    store.mark_agent_delivered(mid)
    return {"ok": True, "id": mid, "state": "delivered", "to": fp}


async def inbox(store, wait_seconds: float = 0.0, limit: int = 50) -> dict:
    """Unread envelopes, long-polling up to `wait_seconds` for one to arrive.

    Fetched messages are marked read: the caller has them, and re-delivering
    them on every call would make an agent answer the same thing twice.
    """
    deadline = time.monotonic() + max(0.0, min(float(wait_seconds or 0), 300.0))
    while True:
        messages = store.agent_inbox(unread_only=True, limit=limit)
        if messages:
            store.mark_agent_read([m["id"] for m in messages])
            return {"ok": True, "messages": messages, "count": len(messages)}
        if time.monotonic() >= deadline:
            return {"ok": True, "messages": [], "count": 0}
        await asyncio.sleep(0.5)


async def flush_outbox(store, ident: dict, limit: int = 20) -> dict:
    """One retry pass over undelivered envelopes. Called by the daemon loop."""
    sent, dropped = 0, 0
    cutoff = int(time.time() * 1000) - RETRY_WINDOW_MS
    for item in store.pending_agent_outbox(limit=limit):
        if item["created"] < cutoff:
            dropped += 1
            continue
        ip, port, _fp = resolve_target(store, item["to"])
        if not ip:
            store.note_agent_attempt(item["id"])
            continue
        intent = agent_intent(ident.get("fingerprint", ""), item["to"],
                              item["id"], item["body"], item["replyTo"],
                              item["created"])
        try:
            await post_intent(ip, intent, port=port or DEVICE_PORT, timeout=5)
        except PeerError:
            store.note_agent_attempt(item["id"])
            continue
        store.mark_agent_delivered(item["id"])
        sent += 1
    return {"sent": sent, "dropped": dropped}
