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
import base64
import contextlib
import hmac
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
from . import ptyshell
from .remoteshell import DEFAULT_TIMEOUT_S, run_command


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
        declared = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_FILE_BYTES
    # 0 (or a negative) must mean "no explicit cap". Clamping it to 1 byte made
    # every incoming file fail on the receiving side with no visible error.
    if declared <= 0:
        return DEFAULT_MAX_FILE_BYTES
    return min(declared, DEFAULT_MAX_FILE_BYTES)


def _device_ip(store, fingerprint: str) -> str:
    row = store.con.execute(
        "SELECT COALESCE(ip,'') FROM persistence_devices WHERE fingerprint=?",
        (fingerprint,)).fetchone()
    return row[0] if row else ""


def _record_peer_ip(store, fingerprint: str, ip: str) -> None:
    """Remember where a paired peer lives; sensitive routes compare against it.

    Never clobbers an existing address: a stale announce must not be able to
    redirect an established peer (discovery poisoning).
    """
    if not fingerprint or not ip:
        return
    had = store.con.execute(
        "SELECT 1 FROM persistence_devices WHERE fingerprint=?",
        (fingerprint,)).fetchone()
    if had:
        store.con.execute(
            "UPDATE persistence_devices SET ip=? WHERE fingerprint=?"
            " AND COALESCE(ip,'')=''", (ip, fingerprint))
    else:
        store.upsert_device(fingerprint, ip=ip, port=DEVICE_PORT)
    store.con.commit()


def _trusted_sender(store, sender, remote) -> bool:
    """Pairing alone is not identity: the fingerprint is public on the wire.

    Sensitive routes require the request to also come from the address we
    learned for that paired device.
    """
    if not isinstance(sender, str) or not sender or not store.is_paired(sender):
        return False
    expected = _device_ip(store, sender)
    return bool(expected) and bool(remote) and expected == remote


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
        sender = bubble["from"]
        # `from` is attacker-controlled in the JSON body. Pairing plus a
        # matching source address is the only identity this transport has.
        if not _trusted_sender(store, sender, request.remote):
            return web.Response(status=403, text="sender not paired")
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
            clipboard_sender = intent.get("deviceId") or (
                extra.get("from") if isinstance(extra, dict) else None)
            if not _trusted_sender(store, clipboard_sender, request.remote):
                return web.Response(status=403, text="sender not paired")
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
            return _pair_intent(intent, request)
        if action == model.ACTION_CONFIRM_RECEIVE:
            # Receiver -> sender "accepted, upload now" ack. The bubble is
            # already armed in file_contents; nothing else to do here.
            return web.Response(text="{0: success}")
        if action == model.ACTION_GROUP_SYNC:
            return _group_sync_intent(intent, store, request.remote)
        if action == model.ACTION_AGENT:
            return _agent_intent(intent, store, request.remote)
        if action == model.ACTION_SHELL:
            return await _shell_intent(intent, store, request.remote)
        return web.Response(status=500, text="receive intent failed")

    def _agent_intent(intent: dict, store, remote=None) -> web.Response:
        """Agent-to-agent envelope (v2K).

        The transport only ever stores text: whatever the two AIs say to each
        other, nothing here executes it. The trust gate is the same pairing
        check the file routes use — an unpaired sender is dropped silently.
        """
        extra = intent.get("extra") if isinstance(intent.get("extra"), dict) else {}
        sender = intent.get("deviceId") or extra.get("from")
        mid = extra.get("id")
        body = extra.get("body")
        reply_to = extra.get("replyTo")
        created = extra.get("created")
        if not (isinstance(mid, str) and mid and isinstance(body, str) and body):
            return web.Response(status=400, text="receive intent failed")
        if not _trusted_sender(store, sender, remote):
            return web.Response(status=403, text="receive intent failed")
        try:
            created_ms = int(created)
        except (TypeError, ValueError):
            created_ms = int(time.time() * 1000)
        store.add_agent_message(mid, "in", sender, body,
                                reply_to if isinstance(reply_to, str) else "",
                                created=created_ms)
        publish("agentMessage", {"id": mid, "from": sender})
        return web.Response(text="{0: success}")

    async def _shell_intent(intent: dict, store, remote=None) -> web.Response:
        """Remote command execution (rdShell, phase 1 = exec only).

        Trust gate matches the other rd* envelopes: an unpaired sender is
        refused before anything is spawned and nothing is recorded for it.
        The command runs in a worker thread so a slow child cannot stall the
        daemon's event loop.
        """
        extra = intent.get("extra") if isinstance(intent.get("extra"), dict) else {}
        sender = intent.get("deviceId") or extra.get("from")
        sid = extra.get("id")
        command = extra.get("command")
        if not (isinstance(sid, str) and sid and isinstance(command, str) and command):
            return web.Response(status=400, text="receive intent failed")
        if not _trusted_sender(store, sender, remote):
            return web.Response(status=403, text="receive intent failed")
        try:
            timeout = float(extra.get("timeout") or DEFAULT_TIMEOUT_S)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_S
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, run_command, command, timeout)
        store.add_shell_audit(sid, sender, command, result)
        return web.json_response({"ok": True, "id": sid, **result})

    active_ptys = {"count": 0}

    async def shell_pty(request: web.Request) -> web.StreamResponse:
        """Interactive PTY over a WebSocket upgrade (rdShell phase 2).

        Same trust gate as exec, plus a source-IP check (the file routes use the
        same one): pairing alone would let any host that knows a fingerprint
        drive a shell.
        """
        sender = request.query.get("from", "")
        if not sender or not store.is_paired(sender):
            return web.json_response({"error": "not-paired"}, status=403)
        row = store.con.execute(
            "SELECT COALESCE(ip,'') FROM persistence_devices WHERE fingerprint=?",
            (sender,)).fetchone()
        expected_ip = row[0] if row else ""
        if expected_ip and request.remote and expected_ip != request.remote:
            return web.json_response(
                {"error": "source-mismatch",
                 "detail": f"expected={expected_ip} remote={request.remote}"},
                status=403)
        if not ptyshell.available():
            return web.json_response(
                {"error": "pty-unavailable",
                 "detail": "install pywinpty on the target"}, status=503)
        if active_ptys["count"] >= ptyshell.MAX_SESSIONS:
            return web.json_response({"error": "busy"}, status=409)
        try:
            cols = int(request.query.get("cols", 80))
            rows = int(request.query.get("rows", 24))
        except (TypeError, ValueError):
            cols, rows = 80, 24
        sid = request.query.get("sid") or uuid.uuid4().hex
        shell = request.query.get("shell", "")

        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        try:
            session = ptyshell.PtySession(shell, cols, rows)
        except ptyshell.PtyUnavailable as exc:
            with contextlib.suppress(Exception):
                await ws.send_json({"t": "error", "error": "pty-unavailable",
                                    "detail": str(exc)})
                await ws.close()
            return ws

        active_ptys["count"] += 1
        started = time.monotonic()
        last = {"t": time.monotonic()}
        exit_code: list[int | None] = [None]
        loop = asyncio.get_running_loop()

        async def pump() -> None:
            while not ws.closed:
                data = await loop.run_in_executor(
                    None, session.read, ptyshell.READ_CHUNK)
                if data:
                    last["t"] = time.monotonic()
                    with contextlib.suppress(Exception):
                        await ws.send_json(
                            {"t": "data", "b": base64.b64encode(data).decode()})
                    continue
                if not session.isalive():
                    exit_code[0] = session.exit_code
                    with contextlib.suppress(Exception):
                        await ws.send_json({"t": "exit", "code": exit_code[0]})
                    return
                await asyncio.sleep(0.05)

        async def watch_idle() -> None:
            while not ws.closed:
                await asyncio.sleep(15)
                if time.monotonic() - last["t"] > ptyshell.MAX_IDLE_S:
                    with contextlib.suppress(Exception):
                        await ws.send_json({"t": "exit", "code": None,
                                            "reason": "idle-timeout"})
                        await ws.close()
                    return

        tasks = [asyncio.create_task(pump()), asyncio.create_task(watch_idle())]
        try:
            async for msg in ws:
                if msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                    break
                if msg.type != web.WSMsgType.TEXT:
                    continue
                try:
                    frame = json.loads(msg.data)
                except ValueError:
                    continue
                kind = frame.get("t")
                last["t"] = time.monotonic()
                if kind == "data":
                    session.write(base64.b64decode(frame.get("b") or ""))
                elif kind == "resize":
                    session.resize(frame.get("cols", cols), frame.get("rows", rows))
                elif kind == "close":
                    break
        finally:
            for task in tasks:
                task.cancel()
            session.close()
            active_ptys["count"] -= 1
            store.add_shell_audit(
                sid, sender, f"pty:{shell or 'default'}",
                {"exit": exit_code[0],
                 "durationMs": int((time.monotonic() - started) * 1000),
                 "truncated": False, "timeout": False})
            with contextlib.suppress(Exception):
                await ws.close()
        return ws

    def _pair_intent(intent: dict, request: web.Request) -> web.Response:
        # Pairing rides /intent [RT 2026-10-21]: askPairDevice/confirmPairDevice
        # with extra {verifyDevice, verifyCode, accept?}.
        # askPairDevice (A asks B): verifyDevice must equal OUR fp, else
        #   `{302: device not match return  verifyDevice = X did = Y}`; on
        #   success B writes pair_devices[A=deviceId]. verifyCode carries OUR
        #   4-digit key (the code shown in our QR), so it is checkable.
        # confirmPairDevice (B replies to A): official sends deviceId=B_fp,
        #   verifyDevice=B_fp, accept="true" (string!) [RT wire_capture] and
        #   its own key in verifyCode, which we cannot verify. Instead require
        #   the source address to match the address we already learned for B.
        # Replies are Dart map toString: `{0: success}` / `{301: ...}` / `{302: ...}`.
        device_id = intent.get("deviceId")
        extra = intent.get("extra") if isinstance(intent.get("extra"), dict) else {}
        verify_device = extra.get("verifyDevice")
        verify_code = extra.get("verifyCode")
        accept = extra.get("accept", True)
        if isinstance(accept, str):
            accept = accept.lower() == "true"
        remote = request.remote
        if intent.get("action") == model.ACTION_ASK_PAIR:
            if not isinstance(verify_device, str) or not verify_device:
                return web.Response(text="{301: pair Device Null}")
            if verify_device != ident.get("fingerprint"):
                return web.Response(
                    text=f"{{302: device not match return  verifyDevice = "
                         f"{verify_device} did = {ident.get('fingerprint')}}}")
            own_key = store.own_pair_key()
            supplied = verify_code if isinstance(verify_code, str) else ""
            if not own_key or not hmac.compare_digest(supplied, own_key):
                publish("pairRejected", {"fingerprint": str(device_id or ""),
                                         "reason": "bad-code"})
                return web.Response(text="{302: verifyCode mismatch}")
            if accept is False:
                return web.Response(text="{0: success}")
            if isinstance(device_id, str) and device_id:
                store.add_pair(device_id, own_key)
                _record_peer_ip(store, device_id, remote)
                publish("pairChanged", {"fingerprint": device_id,
                                        "action": intent.get("action")})
            return web.Response(text="{0: success}")
        # confirmPairDevice: the peer confirms the pair (clipboard-share UI
        # "允许" sends this with its own fp in both deviceId and verifyDevice).
        who = verify_device if isinstance(verify_device, str) and verify_device \
            else device_id
        if not isinstance(who, str) or not who:
            return web.Response(text="{301: pair Device Null}")
        known_ip = _device_ip(store, who)
        if not known_ip or not remote or known_ip != remote:
            publish("pairRejected", {"fingerprint": who,
                                     "reason": "unknown-source"})
            return web.Response(text="{302: pair requires a known source}")
        if accept is not False:
            store.add_pair(who, store.own_pair_key())
            publish("pairChanged", {"fingerprint": who, "action": "confirm"})
        return web.Response(text="{0: success}")

    def _group_sync_intent(intent: dict, store, remote=None) -> web.Response:
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
        if not _trusted_sender(store, sender, remote):
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


    def _wire_log_file() -> Path | None:
        """Where the verbatim wire capture goes.

        ``FLIX_WIRE_LOG`` overrides the path (``off`` disables it); the default
        lives in the app state dir so the server is portable — an absolute
        reverse-engineering path used to make every request 500 off-Windows.
        """
        raw = os.environ.get("FLIX_WIRE_LOG", "").strip()
        if raw.lower() in {"off", "0", "none"}:
            return None
        if raw:
            return Path(raw)
        from .config import APP_DIR
        return APP_DIR / "wire_capture.log"

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
        target = _wire_log_file()
        if target is not None:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "ab") as f:
                    f.write(rec.encode("utf-8", "replace"))
            except OSError:
                # Diagnostics must never break protocol handling.
                pass
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
    app.router.add_get("/shell/pty", shell_pty)
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
