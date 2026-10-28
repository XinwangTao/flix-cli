"""Group chat over the confirmed wire: bubble + groupId, sent per member.

Facts (reverse/protocol.md §8): group messages are ordinary bubbles carrying
the group's uuid in `groupId`, delivered once per member (`to_device` stays a
member fingerprint). The official client accepts such bubbles from any device.
Group creation/join handshake of the official app is not yet decoded —
flixcli groups are managed locally and joined by explicit `group join`.
"""
from __future__ import annotations

import time
import uuid

from . import DEVICE_PORT
from .channel import PeerError, post_bubble
from .model import text_bubble
from .routing import resolve_target


def find_group(store, gid_or_name: str) -> dict | None:
    for g in store.groups():
        if g["id"] == gid_or_name or g["name"] == gid_or_name:
            return g
    return None


def _mention_targets(members: list[str], text: str) -> list[str]:
    """Resolve @<fingerprint prefix> mentions without changing wire shape."""
    tokens = {part[1:].rstrip(",.!?:;，。！？：；")
              for part in text.split() if part.startswith("@") and len(part) > 1}
    if not tokens:
        return members
    selected = [member for member in members
                if any(member.startswith(token) for token in tokens)]
    return selected or members


async def send_group_text(store, ident, gid_or_name: str, text: str) -> dict:
    g = find_group(store, gid_or_name)
    if g is None:
        return {"ok": False, "error": "no-group",
                "detail": f"unknown group {gid_or_name!r}; see flix group list"}
    me = ident["fingerprint"]
    members = _mention_targets([m for m in g["members"] if m != me], text)
    results = []
    delivered = 0
    for m in members:
        ip, port, fp = resolve_target(store, m)
        if not ip:
            results.append({"to": m, "ok": False, "error": "no-device"})
            continue
        bubble = text_bubble(me, fp, text, group_id=g["id"])
        try:
            reply = await post_bubble(ip, bubble, port=port or DEVICE_PORT)
            results.append({"to": m, "ok": True, "reply": reply})
            delivered += 1
        except PeerError as e:
            results.append({"to": m, "ok": False, "error": e.kind,
                            "detail": e.detail})
    if delivered:
        # sender-side record: each machine's group history then shows the
        # full thread, not just received halves [RT 2026-10-22, #3 follow-up]
        b = text_bubble(me, "group", text, group_id=g["id"])
        store.add_text_bubble(b["id"], me, "group", text, b["time"], g["id"])
    return {"ok": delivered > 0, "group": g["id"], "name": g["name"],
            "delivered": delivered, "total": len(members), "results": results}


def new_group(store, name: str, members: list[str]) -> dict:
    gid = str(uuid.uuid4())
    store.create_group(gid, name)
    me = None
    try:
        from .config import load_or_create_identity
        me = load_or_create_identity(store)["fingerprint"]
    except Exception:
        me = None
    for m in members:
        store.add_group_member(gid, m)
    if me and me not in members:
        store.add_group_member(gid, me)
    return {"id": gid, "name": name, "members": store.group_members(gid)}
