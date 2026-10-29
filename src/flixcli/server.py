"""Device channel HTTP server (TCP 8891 by default) mirroring official semantics.

Confirmed official behaviors (reverse/protocol.md §2-§3, §8):
- POST /bubble  -> 200 "received bubble" (plain text); text stored, file
                   bubbles (type=3, content={state:int, meta:{...}}) recorded
                   as pending transfers.
- POST /intent  -> 200 literal "{0: success}" on clipboard intent;
                   500 "receive intent failed" on decode failure.
- POST /file?share_id=<rid>&file_name=<name> -> raw chunked body written to
                   the save dir when a pending file bubble has that rid; 200.
- unknown route -> 404 "Route not found" (plain text).
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import time
import uuid
from pathlib import Path
from typing import Callable

from aiohttp import web

from . import DEVICE_PORT, model
from .media_session import SessionError


def _to_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _to_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


DEFAULT_MAX_FILE_BYTES = 4 * 1024 * 1024 * 1024


def _save_dir(store) -> str:
    p = store.prefs()
    d = p.get("saveDir") or os.path.join(os.path.expanduser("~"), "Downloads")
    os.makedirs(d, exist_ok=True)
    return d


def _max_file_bytes(store) -> int:
    value = store.prefs().get("max_receive_bytes", DEFAULT_MAX_FILE_BYTES)
    try:
        return max(1, min(int(value), DEFAULT_MAX_FILE_BYTES))
    except (TypeError, ValueError):
        return DEFAULT_MAX_FILE_BYTES


def _safe_destination(store, name: str) -> tuple[str, str] | None:
    clean = os.path.basename(name.replace("\\", "/"))
    if not clean or clean in (".", ".."):
        return None
    root = os.path.realpath(_save_dir(store))
    dest = os.path.realpath(os.path.join(root, clean))
    try:
        if os.path.commonpath((root, dest)) != root:
            return None
    except ValueError:
        return None
    return root, dest


def make_app(store, ident: dict, publish: Callable[[str, dict], None],
             clipboard_setter=None, device_port: int = DEVICE_PORT,
             signal_hub=None, media_manager=None, media_runtime=None) -> web.Application:
    async def ping_v2_post(request: web.Request) -> web.Response:
        # Manual-add handshake [RT 2026-10-21]: the official client ignores
        # the request body except `from` (echoed back) and answers with its
        # own deviceModal double-encoded; an empty deviceModal -> 400.
        try:
            payload = await request.json()
        except Exception:
            payload = None
        modal = payload.get("deviceModal") if isinstance(payload, dict) else None
        if not isinstance(modal, dict) or not modal:
            return web.Response(status=400, text="Bad Request")
        peer_from = modal.get("from") if isinstance(modal.get("from"), str) else ""
        own = model.device_modal(ident, device_port, from_=peer_from)
        return web.Response(text=json.dumps(json.dumps(own, separators=(",", ":")),
                                            separators=(",", ":")),
                            content_type="text/plain", charset="utf-8")

    async def bubble_post(request: web.Request) -> web.Response:
        try:
            bubble = await request.json()
        except Exception:
            bubble = None
        if not isinstance(bubble, dict) or not isinstance(bubble.get("id"), str) \
                or not isinstance(bubble.get("from"), str):
            return web.Response(status=400, text="receive bubble failed")
        btype = bubble.get("type")
        content = bubble.get("content")
        ts = _to_int(bubble.get("time"), int(time.time() * 1000))
        gid = bubble.get("groupId") if isinstance(bubble.get("groupId"), str) else ""
        if btype == model.BUBBLE_TEXT and isinstance(content, str):
            store.add_text_bubble(bubble["id"], bubble["from"],
                                  str(bubble.get("to") or ""), content, ts, gid)
            publish("bubbleChanged", {"bubbleId": bubble["id"], "type": "text",
                                      "groupId": gid})
        elif btype == model.BUBBLE_FILE and isinstance(content, dict):
            meta = content.get("meta") if isinstance(content.get("meta"), dict) else {}
            name = str(meta.get("name") or "")
            if not name:
                return web.Response(status=400, text="receive bubble failed")
            rid = str(meta.get("resourceId") or bubble["id"])
            store.con.execute(
                "INSERT OR REPLACE INTO file_contents (id, group_id, resource_id,"
                " name, mime_type, name_with_suffix, size, path, state, progress,"
                " width, height, waiting_for_accept)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (bubble["id"], gid, rid, name, str(meta.get("mimeType") or ""),
                 str(meta.get("nameWithSuffix") or name),
                 _to_int(meta.get("size")), None, _to_int(content.get("state")),
                 _to_float(content.get("progress")), _to_int(meta.get("width")),
                 _to_int(meta.get("height")),
                 1 if content.get("waitingForAccept") else 0))
            store.con.execute(
                "INSERT OR REPLACE INTO bubble_entities (id, from_device, to_device,"
                " type, time, group_id) VALUES (?,?,?,3,?,?)",
                (bubble["id"], bubble["from"], str(bubble.get("to") or ""), ts, gid))
            store.con.commit()
            publish("bubbleChanged", {"bubbleId": bubble["id"], "type": "file",
                                      "groupId": gid})
            # Only a paired sender may auto-arm the raw /file phase. Unpaired
            # bubbles remain visible for a later user decision but cannot write.
            if store.is_paired(bubble["from"]):
                sender_port = store.con.execute(
                    "SELECT port FROM persistence_devices WHERE fingerprint=?",
                    (bubble["from"],)).fetchone()
                intent = model.confirm_receive_intent(ident.get("fingerprint", ""),
                                                      bubble["id"])

                async def _confirm(ip: str, port: int) -> None:
                    from .channel import post_intent
                    try:
                        await post_intent(ip, intent, port=port or DEVICE_PORT)
                    except Exception:
                        pass

                ip = request.remote if isinstance(request.remote, str) else ""
                if ip:
                    import asyncio as _aio
                    _aio.create_task(_confirm(ip, int(sender_port[0])
                                              if sender_port and sender_port[0]
                                              else DEVICE_PORT))
        return web.Response(text="received bubble")

    async def intent_post(request: web.Request) -> web.Response:
        try:
            intent = await request.json()
        except Exception:
            intent = None
        if not isinstance(intent, dict):
            return web.Response(status=500, text="receive intent failed")
        action = intent.get("action")
        if action == model.ACTION_CLIPBOARD:
            extra = intent.get("extra")
            text = extra.get("text") if isinstance(extra, dict) else None
            if isinstance(text, str):
                content = None
                try:
                    payload = json.loads(text)
                    if isinstance(payload, dict):
                        content = payload.get("text")
                except ValueError:
                    content = None
                if isinstance(content, str) and clipboard_setter is not None:
                    await clipboard_setter(content)
                    return web.Response(text="{0: success}")
            return web.Response(status=500, text="receive intent failed")
        if action in (model.ACTION_ASK_PAIR, model.ACTION_CONFIRM_PAIR):
            return _pair_intent(intent)
        if action == model.ACTION_CONFIRM_RECEIVE:
            # Receiver -> sender "accepted, upload now" ack. The bubble is
            # already armed in file_contents; nothing else to do here.
            return web.Response(text="{0: success}")
        if action == model.ACTION_GROUP_SYNC:
            return _group_sync_intent(intent, store)
        return web.Response(status=500, text="receive intent failed")

    def _pair_intent(intent: dict) -> web.Response:
        # Pairing rides /intent [RT 2026-10-21]: askPairDevice/confirmPairDevice
        # with extra {verifyDevice, verifyCode, accept?}.
        # askPairDevice (A asks B): verifyDevice must equal OUR fp, else
        #   `{302: device not match return  verifyDevice = X did = Y}`; on
        #   success B writes pair_devices[A=deviceId].
        # confirmPairDevice (B replies to A): official sends deviceId=B_fp,
        #   verifyDevice=B_fp, accept="true" (string!) [RT wire_capture] —
        #   no self-check; receiver writes pair_devices[verifyDevice].
        # Replies are Dart map toString: `{0: success}` / `{301: ...}` / `{302: ...}`.
        device_id = intent.get("deviceId")
        extra = intent.get("extra") if isinstance(intent.get("extra"), dict) else {}
        verify_device = extra.get("verifyDevice")
        accept = extra.get("accept", True)
        if isinstance(accept, str):
            accept = accept.lower() == "true"
        if intent.get("action") == model.ACTION_ASK_PAIR:
            if not isinstance(verify_device, str) or not verify_device:
                return web.Response(text="{301: pair Device Null}")
            if verify_device != ident.get("fingerprint"):
                return web.Response(
                    text=f"{{302: device not match return  verifyDevice = "
                         f"{verify_device} did = {ident.get('fingerprint')}}}")
            if accept is False:
                return web.Response(text="{0: success}")
            if isinstance(device_id, str) and device_id:
                store.add_pair(device_id, store.own_pair_key())
                publish("pairChanged", {"fingerprint": device_id,
                                        "action": intent.get("action")})
            return web.Response(text="{0: success}")
        # confirmPairDevice: the peer confirms the pair (clipboard-share UI
        # "允许" sends this with its own fp in both deviceId and verifyDevice).
        who = verify_device if isinstance(verify_device, str) and verify_device \
            else device_id
        if not isinstance(who, str) or not who:
            return web.Response(text="{301: pair Device Null}")
        if accept is not False:
            store.add_pair(who, store.own_pair_key())
            publish("pairChanged", {"fingerprint": who, "action": "confirm"})
        return web.Response(text="{0: success}")

    def _group_sync_intent(intent: dict, store) -> web.Response:
        """Group metadata snapshot (v2C). Trust gate: the sender must be paired
        or already a member; anything else is dropped without touching state."""
        extra = intent.get("extra") if isinstance(intent.get("extra"), dict) else {}
        gid = extra.get("groupId")
        name = extra.get("name")
        members = extra.get("members")
        sender = intent.get("deviceId") or extra.get("from")
        try:
            rev = int(extra.get("rev", 0))
            ok = (isinstance(gid, str) and bool(uuid.UUID(gid))
                  and isinstance(name, str) and bool(name.strip())
                  and isinstance(members, list) and bool(members)
                  and all(isinstance(m, str) and m for m in members)
                  and rev > 0)
        except (TypeError, ValueError, AttributeError):
            ok = False
        if not ok:
            return web.Response(status=400, text="receive intent failed")
        if not (store.is_paired(sender) or sender in store.group_members(gid)):
            return web.Response(status=400, text="receive intent failed")
        if store.merge_group_sync(gid, name.strip(), list(dict.fromkeys(members)), rev):
            publish("groupsChanged", {"groupId": gid})
        return web.Response(text="{0: success}")

    async def file_post(request: web.Request) -> web.Response:
        rid = request.query.get("share_id", "")
        supplied_name = request.query.get("file_name", "") or ""
        row = store.con.execute(
            "SELECT f.name, f.size, f.path, b.from_device, f.waiting_for_accept "
            "FROM file_contents f JOIN bubble_entities b ON b.id=f.id "
            "WHERE f.resource_id=?",
            (rid,)).fetchone()
        if not row:
            return web.Response(status=404, text="Route not found")
        expected_name, declared, existing_path, sender, waiting = row
        if not store.is_paired(str(sender)):
            return web.Response(status=403, text="sender not paired")
        known = store.con.execute(
            "SELECT COALESCE(ip,'') FROM persistence_devices WHERE fingerprint=?", (sender,)
        ).fetchone()
        expected_ip = known[0] if known else ""
        if expected_ip and request.remote and expected_ip != request.remote:
            return web.Response(status=403, text=(
                f"source mismatch: expected={expected_ip} remote={request.remote}"))
        if existing_path or not waiting:
            return web.Response(status=409, text="transfer already committed")
        safe = _safe_destination(store, expected_name)
        if safe is None or os.path.basename(supplied_name.replace("\\", "/")) != os.path.basename(expected_name):
            return web.Response(status=400, text="bad file name")
        root, dest = safe
        if os.path.exists(dest):
            return web.Response(status=409, text="destination exists")
        declared = _to_int(declared, -1)
        maximum = _max_file_bytes(store)
        if declared < 0 or declared > maximum:
            return web.Response(status=413, text="file too large")
        temp_dir = os.path.join(root, ".flix-incoming")
        os.makedirs(temp_dir, exist_ok=True)
        dest_tmp = os.path.join(temp_dir, f"{uuid.uuid4().hex}.part")
        got = 0
        try:
            with open(dest_tmp, "xb") as f:
                async for chunk in request.content.iter_chunked(256 * 1024):
                    got += len(chunk)
                    if got > declared or got > maximum:
                        raise ValueError("payload exceeds declared size")
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
            if got != declared:
                raise ValueError(f"size mismatch: {got} != {declared}")
            if os.path.exists(dest):
                raise FileExistsError(dest)
            os.replace(dest_tmp, dest)
        except (ConnectionResetError, asyncio.CancelledError):
            with contextlib.suppress(OSError):
                os.unlink(dest_tmp)
            return web.Response(status=400, text="transfer aborted")
        except (OSError, ValueError) as error:
            with contextlib.suppress(OSError):
                os.unlink(dest_tmp)
            return web.Response(status=400, text=str(error))
        store.con.execute(
            "UPDATE file_contents SET path=?, progress=1.0, waiting_for_accept=0 WHERE resource_id=?",
            (dest, rid))
        store.con.commit()
        publish("fileReceived", {"resourceId": rid, "name": expected_name,
                                 "size": declared, "received": got, "path": dest})
        return web.Response(text="ok")

    async def group_command_post(request: web.Request) -> web.Response:
        # LAN group sync channel [RT 2026-10-21]: "join group" UI sends
        # {"action":"resolve","code":"<4-digit group number>"} to EVERY online
        # device; the group owner answers with the group info, others 404
        # {"error":"找不到群组"} (verbatim official error shape).
        try:
            payload = await request.json()
        except Exception:
            payload = None
        action = payload.get("action") if isinstance(payload, dict) else None
        if action == "resolve":
            code = payload.get("code")
            g = store.group_by_code(str(code)) if code is not None else None
            if not g:
                return web.Response(status=404,
                                    text='{"error":"找不到群组"}',
                                    content_type="application/json")
            return web.Response(text=json.dumps({
                "action": "resolve",
                "code": str(code),
                "data": {
                    "groupId": g["id"],
                    "groupName": g["name"],
                    "members": g["members"],
                },
            }), content_type="application/json")
        return web.Response(status=404, text='{"error":"找不到群组"}',
                            content_type="application/json")

    async def media_session_post(request: web.Request) -> web.Response:
        if media_manager is None:
            return web.json_response({"ok": False, "error": "media-disabled"}, status=503)
        try:
            body = await request.json()
            action = body.get("action")
            peer = str(body.get("from") or "")
            if action == "request":
                session = media_manager.request(peer, body.get("capabilities") or ["view"],
                                                incoming=True,
                                                call_id=str(body.get("callId") or "") or None,
                                                source_ip=request.remote,
                                                peer_public_key=str(body.get("publicKey") or ""))
                return web.json_response({"ok": True, "action": "requested", "session": session})
            if action == "decision":
                call_id = str(body.get("callId") or "")
                current = media_manager.require(call_id)
                sender = str(body.get("from") or "")
                expected_sender = current.initiator if current.receiver == ident.get("fingerprint") else current.receiver
                if sender != expected_sender:
                    raise ValueError("decision sender mismatch")
                expected_ip = media_manager.peer_address(sender)
                if expected_ip and request.remote and expected_ip != request.remote:
                    raise SessionError("source-mismatch",
                                       "decision source IP does not match paired device")
                if current.receiver == ident.get("fingerprint"):
                    raise ValueError("receiver decisions must come from local API")
                if body.get("approve") is True:
                    decision = media_manager.accept_decision(
                        call_id, body.get("capabilities") or ["view"],
                        str(body.get("publicKey") or ""),
                        float(body.get("expiresAt") or 0) / 1000)
                    return web.json_response({"ok": True, "action": "approved", "session": decision})
                denied = media_manager.accept_denial(call_id, str(body.get("reason") or "denied"))
                return web.json_response({"ok": True, "action": "denied", "session": denied})
            return web.json_response({"ok": False, "error": "unknown-action"}, status=400)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return web.json_response({"ok": False, "error": "bad-json", "detail": str(exc)}, status=400)
        except SessionError as exc:
            return web.json_response({"ok": False, "error": exc.kind,
                                      "detail": exc.detail}, status=403)

    async def media_signal_post(request: web.Request) -> web.Response:
        if media_manager is None:
            return web.json_response({"ok": False, "error": "media-disabled"}, status=503)
        try:
            body = await request.json()
            session = media_manager.verify_signal(body, source_ip=request.remote)
            signal_type = body.get("type")
            if signal_type == "offer":
                if media_runtime is None:
                    return web.json_response({"ok": False, "error": "media-disabled"}, status=503)
                peer = media_runtime.create(session.call_id)
                answer = await peer.answer(body.get("payload") or {},
                                           share_screen="view" in session.approved)
                envelope = media_manager.next_signal(session.call_id, "answer", answer)
                return web.json_response({"ok": True, "callId": session.call_id,
                                          "signal": envelope})
            if signal_type == "answer":
                if media_runtime is None:
                    return web.json_response({"ok": False, "error": "media-disabled"}, status=503)
                await media_runtime.get(session.call_id).apply_answer(body.get("payload") or {})
            elif signal_type == "hangup":
                media_manager.revoke(session.call_id, "peer hangup")
            media_manager._emit(session, "signal-received")
            return web.json_response({"ok": True, "callId": session.call_id})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return web.json_response({"ok": False, "error": "bad-json", "detail": str(exc)}, status=400)
        except SessionError as exc:
            return web.json_response({"ok": False, "error": exc.kind,
                                      "detail": exc.detail}, status=403)

    async def not_found(request: web.Request) -> web.Response:
        return web.Response(status=404, text="Route not found")


    @web.middleware
    async def _wire_log(request, handler):
        import time as _t
        if request.path == "/file":
            # /file is a streaming upload; reading here would consume the body
            # before file_post can enforce size and commit it.
            body_repr = "<streaming file body redacted>"
        else:
            try:
                body = await request.read()
            except Exception:
                body = b"?"
            if request.path.startswith("/webrtc/v1/"):
                import hashlib as _hashlib
                digest = _hashlib.sha256(body).hexdigest()[:16]
                body_repr = f"<redacted media envelope bytes={len(body)} sha256={digest}>"
            else:
                body_repr = repr(body[:2000])
        rec = (f"[{_t.strftime('%H:%M:%S')}] {request.method} {request.path_qs}\n"
               f"headers={dict(request.headers)}\n"
               f"body={body_repr}\n----\n")
        with open(r"D:/flix-cli/reverse/wire_capture.log", "ab") as f:
            f.write(rec.encode("utf-8", "replace"))
        return await handler(request)

    app = web.Application(middlewares=[_wire_log])

    app.router.add_post("/ping_v2", ping_v2_post)
    if media_manager is not None:
        app.router.add_post("/webrtc/v1/session", media_session_post)
        app.router.add_post("/webrtc/v1/signal", media_signal_post)
    if signal_hub is not None:
        from .webrtc import make_signal_app
        app.add_subapp("/webrtc", make_signal_app(signal_hub))
    app.router.add_post("/group/v1/command", group_command_post)
    app.router.add_post("/bubble", bubble_post)
    app.router.add_post("/intent", intent_post)
    app.router.add_post("/file", file_post)
    app.router.add_route("*", "/{tail:.*}", not_found)
    return app


async def serve_channel(store, ident, port: int, clipboard_setter=None,
                        publish: Callable[[str, dict], None] | None = None,
                        signal_hub=None, media_manager=None, media_runtime=None):
    if publish is None:
        publish = lambda t, p: None  # noqa: E731
    app = make_app(store, ident, publish, clipboard_setter, device_port=port,
                   signal_hub=signal_hub, media_manager=media_manager,
                   media_runtime=media_runtime)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner, publish
