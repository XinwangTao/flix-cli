"""Desktop API compatible with the official 127.0.0.1:8898 surface.

Shapes captured live from the official client (protocol.md §5):
- GET /api/self    -> {"id","alias","deviceModel","deviceType","ip","port","online","paired"}
- GET /api/devices -> [device...]
- GET /api/settings-> {"autoReceive","saveDir","theme","enableMdns"}
- GET /api/events  -> SSE, "retry: 2000", events {"type":...,"payload":...}
- POST /api/send-text, /api/send-files, /api/pair, /api/respond-pair,
  /api/accept-transfer, /api/cancel-transfer, /api/webrtc/*
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Callable

from aiohttp import web

from . import local_ip


def make_desktop_app(store, ident: dict, send_text_fn: Callable,
                     api_port: int = 8898,
                     channel_port: int = 8891,
                     webrtc_hub=None) -> tuple[web.Application, Callable[[dict], None]]:
    events_q: asyncio.Queue = asyncio.Queue(maxsize=256)
    shutdown_ev = asyncio.Event()

    def publish(evt: dict) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            events_q.put_nowait(evt)

    async def on_shutdown(app: web.Application) -> None:
        shutdown_ev.set()

    async def self_get(request: web.Request) -> web.Response:
        return web.json_response({
            "id": ident["fingerprint"], "alias": ident["alias"],
            "deviceModel": ident.get("deviceModel", "Windows"),
            "deviceType": ident.get("deviceType", "desktop"),
            "ip": local_ip(), "port": channel_port,
            "online": True, "paired": False,
        })

    async def devices_get(request: web.Request) -> web.Response:
        return web.json_response(store.devices())

    async def settings_get(request: web.Request) -> web.Response:
        p = store.prefs()
        return web.json_response({
            "autoReceive": p.get("autoReceive", True),
            "saveDir": p.get("saveDir", ""),
            "theme": p.get("theme", "system"),
            "enableMdns": p.get("enableMdns", True),
        })

    async def bubbles_get(request: web.Request) -> web.Response:
        return web.json_response(store.bubbles(50))

    async def events_get(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                           "Cache-Control": "no-cache"})
        await resp.prepare(request)
        await resp.write(b"retry: 2000\n\n")
        try:
            while not shutdown_ev.is_set():
                if request.transport is not None and request.transport.is_closing():
                    break
                try:
                    evt = await asyncio.wait_for(events_q.get(), timeout=5)
                    await resp.write(b"data: " + json.dumps(evt).encode() + b"\n\n")
                except asyncio.TimeoutError:
                    await resp.write(b": keepalive\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    async def send_text_post(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict) or not body.get("to") or not isinstance(body.get("text"), str):
            return web.json_response({"error": "bad params"}, status=400)
        result = await send_text_fn(body["to"], body["text"])
        return web.json_response(result)

    async def stub_post(request: web.Request) -> web.Response:
        return web.json_response({"error": "not implemented in flix-cli yet",
                                  "route": request.path}, status=501)

    app = web.Application()
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/api/self", self_get)
    app.router.add_get("/api/devices", devices_get)
    app.router.add_get("/api/settings", settings_get)
    app.router.add_get("/api/bubbles", bubbles_get)
    app.router.add_get("/api/events", events_get)
    app.router.add_post("/api/send-text", send_text_post)
    for route in ("/api/pair", "/api/respond-pair", "/api/send-files",
                  "/api/accept-transfer", "/api/cancel-transfer"):
        app.router.add_post(route, stub_post)

    # /api/webrtc/* — real signaling-plane handlers over the shared hub
    # [compat: media transport not implemented; official TransAction names
    # for offer/answer/ice are still unknown (protocol.md §11)].
    async def webrtc_post(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
        stype = request.path.rsplit("/", 1)[-1]
        target = payload.get("deviceId") or ""
        sig = {"from": ident["fingerprint"], "to": target, "type": stype,
               "callId": payload.get("callId"),
               "payload": payload.get("payload") or payload.get("sdp") or {}}
        result = webrtc_hub.apply(sig)
        if target:
            from .channel import post_json
            row = store.con.execute(
                "SELECT COALESCE(ip,''), COALESCE(port, 0) FROM persistence_devices"
                " WHERE fingerprint LIKE ?", (target[:8] + "%",)).fetchone()
            if row and row[0]:
                import asyncio as _aio
                from . import DEVICE_PORT

                async def _push():
                    try:
                        await post_json(row[0], "/webrtc/signal",
                                        {"from": ident["fingerprint"], **sig},
                                        port=int(row[1] or DEVICE_PORT))
                    except Exception:
                        pass
                _aio.create_task(_push())
        return web.json_response({"ok": True, **result})

    for route in ("/api/webrtc/offer", "/api/webrtc/answer",
                  "/api/webrtc/ice", "/api/webrtc/hangup"):
        app.router.add_post(route, webrtc_post)
    return app, publish


async def serve_desktop_api(store, ident, send_text_fn, port: int,
                            channel_port: int = 8891, webrtc_hub=None):
    app, publish = make_desktop_app(store, ident, send_text_fn,
                                    api_port=port, channel_port=channel_port,
                                    webrtc_hub=webrtc_hub)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner, publish
