"""WebRTC signaling compatibility layer (flixcli ↔ flixcli).

The official TransAction names for offer/answer/ice are still unknown
(protocol.md §11); until they are decoded this module provides a
flixcli-to-flixcli signaling channel over the device channel:

    POST /webrtc/signal  {"from":fp, "to":fp, "type":"offer|answer|ice|hangup",
                          "callId":uuid, "payload":{...}}
        -> 200 {"ok":true,"session":{...}}   (stored + forwarded to `to`)
        -> 404 {"error":"no session"}        on unknown hangup target

Sessions are in-memory; media transport (aiortc) is intentionally out of
scope for now — this is the signaling/state plane only. The Desktop API
/api/webrtc/* endpoints create sessions and push signals to the peer.
"""
from __future__ import annotations

import uuid
from typing import Callable

from aiohttp import web

VALID_TYPES = ("offer", "answer", "ice", "hangup")


class SignalHub:
    """In-memory session table shared by the device channel and Desktop API."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}   # callId -> state
        self.listeners: list[Callable[[dict], None]] = []

    def on_event(self, fn: Callable[[dict], None]) -> None:
        self.listeners.append(fn)

    def emit(self, evt: dict) -> None:
        for fn in self.listeners:
            try:
                fn(evt)
            except Exception:
                pass

    def apply(self, signal: dict) -> dict:
        call = signal.get("callId") or str(uuid.uuid4())
        stype = signal.get("type")
        sig = {"from": signal.get("from"), "type": stype,
               "payload": signal.get("payload") or {}}
        sess = self.sessions.setdefault(call, {"callId": call, "role": {},
                                               "ice": [], "state": "new"})
        peer = sig["from"]
        if stype == "offer":
            sess["role"]["offer"] = sig
            sess["state"] = "have-remote-offer"
        elif stype == "answer":
            sess["role"]["answer"] = sig
            sess["state"] = "connected-signaling"
        elif stype == "ice":
            sess["ice"].append(sig)
        elif stype == "hangup":
            sess["state"] = "closed"
            self.sessions.pop(call, None)
        self.emit({"type": "webrtcSignal", "callId": call, "signal": sig})
        return {"callId": call, "session": sess}


def make_signal_app(hub: SignalHub) -> web.Application:
    app = web.Application()

    async def signal_post(request: web.Request) -> web.Response:
        try:
            signal = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        if signal.get("type") not in VALID_TYPES:
            return web.json_response({"error": f"type must be one of "
                                       f"{VALID_TYPES}"}, status=400)
        result = hub.apply(signal)
        return web.json_response({"ok": True, **result})

    async def sessions_get(request: web.Request) -> web.Response:
        return web.json_response({"sessions": list(hub.sessions.values())})

    app.router.add_post("/signal", signal_post)
    app.router.add_get("/sessions", sessions_get)
    return app
