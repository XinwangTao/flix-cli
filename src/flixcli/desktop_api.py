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
import os
import time
from typing import Callable

from aiohttp import web

from . import local_ip
from .adb import AdbClient, AdbError
from .channel import PeerError, post_intent, post_json
from .media_session import SessionError, SessionManager
from .model import clipboard_intent
from .routing import resolve_target


def _record_sent_file(store, ident: dict, result: dict, path: str) -> None:
    """Mirror a successful send into the local store so the sender sees it."""
    bid = result["id"]
    name = result["name"]
    size = int(result.get("size") or 0)
    store.con.execute(
        "INSERT OR REPLACE INTO bubble_entities (id, from_device, to_device, type,"
        " time, group_id) VALUES (?,?,?,3,?,?)",
        (bid, ident["fingerprint"], result["to"], int(time.time() * 1000), ""))
    store.con.execute(
        "INSERT OR REPLACE INTO file_contents (id, group_id, resource_id, name,"
        " mime_type, name_with_suffix, size, path, state, progress, width, height,"
        " waiting_for_accept) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (bid, "", bid, name, "", name, size, os.path.abspath(path), 2, 1.0, 0, 0, 0))
    store.con.commit()


def make_desktop_app(store, ident: dict, send_text_fn: Callable,
                     api_port: int = 8898,
                     channel_port: int = 8891,
                     webrtc_hub=None,
                     adb_client: AdbClient | None = None,
                     media_manager: SessionManager | None = None,
                     media_runtime=None) -> tuple[web.Application, Callable[[dict], None]]:
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

    async def groups_get(request: web.Request) -> web.Response:
        return web.json_response(store.groups())

    async def group_send_post(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad-json"}, status=400)
        if not isinstance(body, dict) or not body.get("group") or not isinstance(body.get("text"), str):
            return web.json_response({"ok": False, "error": "bad-params"}, status=400)
        from .config import load_or_create_identity
        from .groupchat import send_group_text
        result = await send_group_text(store, load_or_create_identity(store),
                                       str(body["group"]), body["text"])
        return web.json_response(result, status=200 if result.get("ok") else 400)

    async def group_update_post(request: web.Request) -> web.Response:
        """Rename / add / remove members, then push the snapshot to the peers."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad-json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "bad-params"}, status=400)
        target = body.get("group") or body.get("id")
        add, remove = body.get("add") or [], body.get("remove") or []
        if not isinstance(target, str) or not target.strip():
            return web.json_response({"ok": False, "error": "bad-params"}, status=400)
        if not isinstance(add, list) or not isinstance(remove, list):
            return web.json_response({"ok": False, "error": "bad-params"}, status=400)
        from .config import load_or_create_identity
        from .groupchat import update_group
        name = body.get("name") if isinstance(body.get("name"), str) else None
        result = await update_group(store, load_or_create_identity(store), target,
                                    name=name,
                                    add=tuple(str(x) for x in add if str(x)),
                                    remove=tuple(str(x) for x in remove if str(x)))
        return web.json_response(result, status=200 if result.get("ok") else 400)

    async def group_history_get(request: web.Request) -> web.Response:
        from .groupchat import find_group
        group = find_group(store, request.match_info["group"])
        if group is None:
            return web.json_response({"ok": False, "error": "no-group"}, status=404)
        return web.json_response({"ok": True, "group": group,
                                  "messages": store.group_history(group["id"], 50)})

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

    async def send_files_post(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict) or not body.get("to") or not isinstance(body.get("paths"), list):
            return web.json_response({"ok": False, "error": "bad-params"}, status=400)
        ip, port, fp = resolve_target(store, str(body["to"]))
        if not ip:
            return web.json_response({"ok": False, "error": "no-device"}, status=404)
        from .files import send_file
        results = []
        for path in body["paths"]:
            result = await send_file(ip, str(path), from_fp=ident["fingerprint"],
                                     to_fp=fp, port=port)
            if result.get("ok"):
                # Without a local row the sender's own timeline never shows the
                # file it just sent.
                _record_sent_file(store, ident, result, str(path))
                publish({"type": "bubbleChanged",
                         "payload": {"bubbleId": result["id"], "type": "file"}})
            results.append(result)
        ok = bool(results) and all(item.get("ok") for item in results)
        return web.json_response({"ok": ok, "results": results}, status=200 if ok else 400)

    async def send_clipboard_post(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict) or not body.get("to") or not isinstance(body.get("text"), str):
            return web.json_response({"ok": False, "error": "bad-params"}, status=400)
        ip, port, fp = resolve_target(store, str(body["to"]))
        if not ip:
            return web.json_response({"ok": False, "error": "no-device"}, status=404)
        try:
            reply = await post_intent(ip, clipboard_intent(ident["fingerprint"], body["text"]),
                                      port=port)
            return web.json_response({"ok": True, "to": fp, "reply": reply})
        except PeerError as error:
            return web.json_response({"ok": False, "error": error.kind,
                                      "detail": error.detail}, status=400)

    async def overview_get(request: web.Request) -> web.Response:
        settings = store.prefs()
        media = {"sessions": media_manager.list(), "audit": media_manager.audit[-50:]} \
            if media_manager is not None else {"sessions": [], "audit": []}
        adb = {"ok": False, "error": "adb-disabled", "devices": []}
        if adb_client is not None:
            try:
                adb = {"ok": True, "devices": [device.as_dict()
                                                for device in await adb_client.devices()]}
            except AdbError as error:
                adb = {"ok": False, "error": error.kind, "detail": error.detail,
                       "devices": []}
        return web.json_response({
            "self": {"id": ident["fingerprint"], "alias": ident["alias"],
                     "deviceModel": ident.get("deviceModel", "Windows"),
                     "ip": local_ip(), "port": channel_port, "online": True},
            "devices": store.devices(), "bubbles": store.bubbles(100),
            "groups": store.groups(), "media": media, "adb": adb,
            "paired": [row[0] for row in store.con.execute(
                "SELECT fingerprint FROM pair_devices")],
            "settings": {"autoReceive": settings.get("autoReceive", True),
                         "saveDir": settings.get("saveDir", ""),
                         "theme": settings.get("theme", "system")},
        })

    async def adb_devices_get(request: web.Request) -> web.Response:
        if adb_client is None:
            return web.json_response({"ok": False, "error": "adb-disabled"}, status=503)
        try:
            devices = await adb_client.devices()
            return web.json_response({"ok": True, "devices": [d.as_dict() for d in devices]})
        except AdbError as e:
            return web.json_response({"ok": False, "error": e.kind, "detail": e.detail}, status=503)

    async def adb_transfer_post(request: web.Request) -> web.Response:
        if adb_client is None:
            return web.json_response({"ok": False, "error": "adb-disabled"}, status=503)
        try:
            body = await request.json()
            action = body.get("action")
            serial = body.get("serial")
            if action == "push":
                result = await adb_client.push(str(body.get("source", "")),
                                               str(body.get("destination", "")), serial)
            elif action == "pull":
                result = await adb_client.pull(str(body.get("source", "")),
                                               str(body.get("destination", "")), serial)
            else:
                return web.json_response({"ok": False, "error": "transfer-action-required"}, status=400)
            return web.json_response(result)
        except (AdbError, ValueError, AttributeError) as e:
            if isinstance(e, AdbError):
                return web.json_response({"ok": False, "error": e.kind, "detail": e.detail}, status=400)
            return web.json_response({"ok": False, "error": "bad-json", "detail": str(e)}, status=400)

    def media_error(error: SessionError) -> web.Response:
        status = 404 if error.kind in ("no-session", "no-media") else 403 if error.kind in (
            "not-paired", "not-approved", "control-not-approved", "source-mismatch") else 400
        return web.json_response({"ok": False, "error": error.kind,
                                  "detail": error.detail}, status=status)

    async def media_sessions_get(request: web.Request) -> web.Response:
        if media_manager is None:
            return web.json_response({"ok": False, "error": "media-disabled"}, status=503)
        return web.json_response({"ok": True, "sessions": media_manager.list(),
                                  "audit": media_manager.audit[-50:]})

    async def media_request_post(request: web.Request) -> web.Response:
        if media_manager is None:
            return web.json_response({"ok": False, "error": "media-disabled"}, status=503)
        try:
            body = await request.json()
            target = str(body.get("deviceId", ""))
            capabilities = body.get("capabilities") or ["view"]
            result = media_manager.request(target, capabilities)
            row = store.con.execute(
                "SELECT COALESCE(ip,''), COALESCE(port, 0) FROM persistence_devices WHERE fingerprint LIKE ?",
                (target[:8] + "%",)).fetchone()
            if not row or not row[0]:
                raise SessionError("no-device", "paired peer has no known address")
            request_payload = {"action": "request", "from": ident["fingerprint"],
                               "callId": result["callId"], "capabilities": result["requested"],
                               "publicKey": result["publicKey"]}
            try:
                await post_json(row[0], "/webrtc/v1/session", request_payload,
                                port=int(row[1] or 8891))
            except Exception as exc:
                media_manager.revoke(result["callId"], "request delivery failed")
                raise SessionError("unreachable", str(exc)[:160])
            return web.json_response({"ok": True, "session": result})
        except (AttributeError, TypeError, json.JSONDecodeError):
            return web.json_response({"ok": False, "error": "bad-json"}, status=400)
        except SessionError as error:
            return media_error(error)

    async def media_decision_post(request: web.Request) -> web.Response:
        if media_manager is None:
            return web.json_response({"ok": False, "error": "media-disabled"}, status=503)
        try:
            body = await request.json()
            call_id = str(body.get("callId", ""))
            current = media_manager.require(call_id)
            if current.receiver != ident["fingerprint"]:
                raise SessionError("not-receiver", "only the receiving device can decide")
            approved = body.get("approve") is True
            if approved:
                result = media_manager.approve(call_id, body.get("capabilities") or ["view"])
            else:
                result = media_manager.deny(call_id, str(body.get("reason") or "denied by user"))
            peer = current.initiator
            row = store.con.execute(
                "SELECT COALESCE(ip,''), COALESCE(port, 0) FROM persistence_devices WHERE fingerprint LIKE ?",
                (peer[:8] + "%",)).fetchone()
            if not row or not row[0]:
                raise SessionError("no-device", "initiator has no known address")
            decision = {"action": "decision", "from": ident["fingerprint"],
                        "callId": call_id, "approve": approved,
                        "capabilities": result.get("approved", []),
                        "reason": result.get("reason")}
            if approved:
                decision.update(publicKey=result["publicKey"], expiresAt=result["expiresAt"])
            await post_json(row[0], "/webrtc/v1/session", decision,
                            port=int(row[1] or 8891))
            return web.json_response({"ok": True, "session": result})
        except (AttributeError, TypeError, json.JSONDecodeError):
            return web.json_response({"ok": False, "error": "bad-json"}, status=400)
        except SessionError as error:
            return media_error(error)

    async def media_control_post(request: web.Request) -> web.Response:
        if media_manager is None or media_runtime is None:
            return web.json_response({"ok": False, "error": "media-disabled"}, status=503)
        try:
            body = await request.json()
            call_id = str(body.get("callId", ""))
            session = media_manager.require(call_id)
            if session.initiator != ident["fingerprint"] or "control" not in session.approved:
                raise SessionError("control-not-approved", "local session has no control capability")
            media_runtime.get(call_id).send_control(body.get("event") or {})
            return web.json_response({"ok": True})
        except (AttributeError, TypeError, json.JSONDecodeError):
            return web.json_response({"ok": False, "error": "bad-json"}, status=400)
        except SessionError as error:
            return media_error(error)

    async def media_revoke_post(request: web.Request) -> web.Response:
        if media_manager is None:
            return web.json_response({"ok": False, "error": "media-disabled"}, status=503)
        try:
            body = await request.json()
            result = media_manager.revoke(str(body.get("callId", "")),
                                          str(body.get("reason") or "revoked locally"))
            return web.json_response({"ok": True, "session": result})
        except (AttributeError, json.JSONDecodeError):
            return web.json_response({"ok": False, "error": "bad-json"}, status=400)
        except SessionError as error:
            return media_error(error)

    async def media_offer_post(request: web.Request) -> web.Response:
        if media_runtime is None:
            return web.json_response({"ok": False, "error": "media-disabled"}, status=503)
        try:
            body = await request.json()
            call_id = str(body.get("callId", ""))
            session = media_manager.require(call_id)
            if session.initiator != ident["fingerprint"] or session.state != "approved":
                raise SessionError("not-approved")
            peer = media_runtime.create(call_id)
            peer.add_control_channel()
            offer = await peer.offer()
            envelope = media_manager.next_signal(call_id, "offer", offer)
            remote = media_manager.peer(session)
            row = store.con.execute(
                "SELECT COALESCE(ip,''), COALESCE(port, 0) FROM persistence_devices WHERE fingerprint=?",
                (remote,)).fetchone()
            if not row or not row[0]:
                raise SessionError("no-device", "peer has no known address")
            response = await post_json(row[0], "/webrtc/v1/signal", envelope,
                                       port=int(row[1] or 8891), timeout=30)
            answer_signal = response.get("signal") if isinstance(response, dict) else None
            if not isinstance(answer_signal, dict):
                raise SessionError("bad-answer", "peer did not return an authenticated answer")
            media_manager.verify_signal(answer_signal, source_ip=row[0])
            await peer.apply_answer(answer_signal.get("payload") or {})
            return web.json_response({"ok": True, "callId": call_id,
                                      "session": media_manager.require(call_id).public()})
        except SessionError as error:
            return media_error(error)

    async def media_answer_post(request: web.Request) -> web.Response:
        if media_runtime is None:
            return web.json_response({"ok": False, "error": "media-disabled"}, status=503)
        try:
            body = await request.json()
            call_id = str(body.get("callId", ""))
            session = media_manager.require(call_id)
            if session.receiver != ident["fingerprint"] or session.state != "approved":
                raise SessionError("not-approved")
            peer = media_runtime.create(call_id)
            answer = await peer.answer(body.get("offer") or {}, share_screen="view" in session.approved)
            return web.json_response({"ok": True, "callId": call_id, "answer": answer})
        except (KeyError, TypeError):
            return web.json_response({"ok": False, "error": "bad-offer"}, status=400)
        except SessionError as error:
            return media_error(error)

    async def media_apply_answer_post(request: web.Request) -> web.Response:
        if media_runtime is None:
            return web.json_response({"ok": False, "error": "media-disabled"}, status=503)
        try:
            body = await request.json()
            call_id = str(body.get("callId", ""))
            await media_runtime.get(call_id).apply_answer(body.get("answer") or {})
            return web.json_response({"ok": True, "callId": call_id})
        except (KeyError, TypeError):
            return web.json_response({"ok": False, "error": "bad-answer"}, status=400)
        except SessionError as error:
            return media_error(error)

    async def media_snapshot_get(request: web.Request) -> web.StreamResponse:
        if media_runtime is None:
            raise web.HTTPNotFound()
        path = media_runtime.snapshot(request.match_info["call_id"])
        if path is None:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})

    async def stub_post(request: web.Request) -> web.Response:
        return web.json_response({"error": "not implemented in flix-cli yet",
                                  "route": request.path}, status=501)

    app = web.Application()
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/api/overview", overview_get)
    app.router.add_get("/api/self", self_get)
    app.router.add_get("/api/devices", devices_get)
    app.router.add_get("/api/settings", settings_get)
    app.router.add_get("/api/bubbles", bubbles_get)
    app.router.add_get("/api/groups", groups_get)
    app.router.add_get("/api/groups/{group}/history", group_history_get)
    app.router.add_post("/api/groups/send", group_send_post)
    app.router.add_post("/api/groups/update", group_update_post)
    app.router.add_get("/api/events", events_get)
    app.router.add_get("/api/adb/devices", adb_devices_get)
    app.router.add_post("/api/adb/transfer", adb_transfer_post)
    app.router.add_post("/api/send-text", send_text_post)
    app.router.add_post("/api/send-files", send_files_post)
    app.router.add_post("/api/send-clipboard", send_clipboard_post)
    app.router.add_get("/api/media/sessions", media_sessions_get)
    app.router.add_post("/api/media/request", media_request_post)
    app.router.add_post("/api/media/decision", media_decision_post)
    app.router.add_post("/api/media/revoke", media_revoke_post)
    app.router.add_post("/api/media/control", media_control_post)
    app.router.add_post("/api/media/offer", media_offer_post)
    app.router.add_post("/api/media/answer", media_answer_post)
    app.router.add_post("/api/media/apply-answer", media_apply_answer_post)
    app.router.add_get("/api/media/{call_id}/snapshot", media_snapshot_get)
    for route in ("/api/pair", "/api/respond-pair",
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
                            channel_port: int = 8891, webrtc_hub=None,
                            adb_client=None, media_manager=None, media_runtime=None):
    app, publish = make_desktop_app(store, ident, send_text_fn,
                                    api_port=port, channel_port=channel_port,
                                    webrtc_hub=webrtc_hub, adb_client=adb_client,
                                    media_manager=media_manager,
                                    media_runtime=media_runtime)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner, publish
