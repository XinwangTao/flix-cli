"""Async runtime shared by flixd and `flix serve`."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import sys
from pathlib import Path

from . import DEVICE_PORT, local_ip, model
from .adb import AdbClient
from .media_session import SessionManager
from .media_transport import MediaRuntime
from .channel import PeerError, post_bubble, post_intent
from .desktop_api import serve_desktop_api
from .discovery import Discovery
from .server import serve_channel


async def set_clipboard_text(text: str) -> None:
    """Best-effort clipboard write; clip.exe understands UTF-16LE with BOM."""
    if not sys.platform.startswith("win"):
        return
    proc = await asyncio.create_subprocess_exec(
        "clip", stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    data = text.encode("utf-16-le")
    with contextlib.suppress(Exception):
        await proc.communicate(b"\xff\xfe" + data)


def read_clipboard() -> str | None:
    """Local clipboard text via user32 (CF_UNICODETEXT); None if empty/error."""
    if not sys.platform.startswith("win"):
        return None
    import ctypes
    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    CF_UNICODETEXT = 13
    if not u32.OpenClipboard(0):
        return None
    try:
        h = u32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = k32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            k32.GlobalUnlock(h)
    finally:
        u32.CloseClipboard()


async def clip_sync_loop(store, ident: dict, state: dict,
                         interval: float = 1.5) -> None:
    """Official sendClipboard semantics [RT 22:23]: on local clipboard change,
    broadcast a clipboard intent to every paired device. Echoes of our own
    writes (from a remote peer) are suppressed via the shared clip_state."""
    last_sent: str | None = None
    while True:
        await asyncio.sleep(interval)
        text = await asyncio.to_thread(read_clipboard)
        if not text:
            continue
        # clip.exe keeps its UTF-16LE BOM inside the clipboard text; strip it
        # so it never loops back through peers (official echo grew 7 BOMs).
        text = text.lstrip("﻿")
        if not text or text == state.get("last_set") or text == last_sent:
            continue
        last_sent = text
        h = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]
        intent = model.clipboard_intent(ident["fingerprint"], text)
        sent = 0
        for fp, ip, port in store.paired_targets():
            if fp == ident["fingerprint"]:
                continue
            try:
                await post_intent(ip, intent, port=port)
                sent += 1
            except (PeerError, Exception):
                continue
        if sent:
            print(f"[clip-sync] {h} -> {sent} paired device(s)",
                  file=sys.stderr, flush=True)


async def run_daemon(store, ident, device_port: int, api_port: int,
                     announce: bool = True, clip_sync: bool = True,
                     web_port: int = 0, multicast_if: str | None = None) -> int:
    devices: dict[str, dict] = {}

    def on_device(dev: dict) -> None:
        fp = dev.get("fingerprint")
        if not fp or fp == ident["fingerprint"]:
            return
        if devices.get(fp) == dev:
            return
        devices[fp] = dev
        try:
            store.upsert_device(fp, alias=dev.get("alias", ""),
                                device_model=dev.get("deviceModel", ""),
                                port=dev.get("port"), version=dev.get("version"),
                                ip=dev.get("ip", ""), os_name=dev.get("os", ""))
        except Exception as e:
            print(f"[store] {e}", file=sys.stderr, flush=True)
            return
        if api_publish:
            api_publish({"type": "devicesChanged",
                         "payload": list(devices.values())})
        print(f"[discover] {dev.get('alias')} ({fp[:8]}) at "
              f"{dev.get('ip')}:{dev.get('port')}", file=sys.stderr, flush=True)

    def resolve(to: str) -> tuple[str | None, int, str]:
        """fingerprint (exact/prefix) or ip[:port] -> (ip, port, fingerprint)."""
        port = DEVICE_PORT
        target = to
        head, _, tail = to.rpartition(":")
        if tail.isdigit():
            target, port = head, int(tail)
        dev = devices.get(to) or devices.get(target) or {}
        if not dev:
            for row in store.devices():
                if row["fingerprint"].startswith(target) and row.get("ip"):
                    dev = {"ip": row["ip"], "fingerprint": row["fingerprint"],
                           "port": row.get("port") or port}
                    break
        ip = dev.get("ip") or target
        fp = dev.get("fingerprint", to)
        p = dev.get("port") or port
        if not (ip and ip.replace(".", "").isdigit()):
            return None, p, fp
        return ip, p, fp

    async def send_text(to: str, text: str) -> dict:
        ip, port, fp = resolve(to)
        if not ip:
            return {"ok": False, "error": "no-device", "detail": f"unknown target {to!r}"}
        bubble = model.text_bubble(ident["fingerprint"], fp, text)
        try:
            reply = await post_bubble(ip, bubble, port=port)
            store.add_text_bubble(bubble["id"], bubble["from"], bubble["to"],
                                  bubble["content"], bubble["time"],
                                  bubble.get("groupId") or "")
            if api_publish:
                api_publish({"type": "bubbleChanged",
                             "payload": {"bubbleId": bubble["id"], "type": "text"}})
            return {"ok": True, "id": bubble["id"], "reply": reply, "ip": ip}
        except PeerError as e:
            return {"ok": False, "error": e.kind, "detail": e.detail, "ip": ip}

    clip_state = {"last_set": None}

    async def _set_clipboard(text: str) -> None:
        await set_clipboard_text(text)
        clip_state["last_set"] = text   # suppress our own echo in clip_sync

    from .webrtc import SignalHub, make_signal_app
    hub = SignalHub()
    adb_client = AdbClient()

    def peer_ip(fp: str) -> str | None:
        row = store.con.execute(
            "SELECT COALESCE(ip, '') FROM persistence_devices WHERE fingerprint=?", (fp,)
        ).fetchone()
        return row[0] if row and row[0] else None

    api_publish = None
    media_manager = SessionManager(ident["fingerprint"], store.is_paired, peer_ip)
    media_manager.on_event(lambda event: api_publish(event) if api_publish else None)
    media_dir = Path(store.prefs().get("mediaSnapshotDir") or
                     (Path(store.path).parent / "media_snapshots"))
    media_runtime = MediaRuntime(media_manager, media_dir)

    channel_runner, publish = await serve_channel(store, ident, device_port,
                                                  clipboard_setter=_set_clipboard,
                                                  signal_hub=hub,
                                                  media_manager=media_manager,
                                                  media_runtime=media_runtime)
    api_runner, api_publish = await serve_desktop_api(store, ident, send_text,
                                                      api_port,
                                                      channel_port=device_port,
                                                      webrtc_hub=hub,
                                                      adb_client=adb_client,
                                                      media_manager=media_manager,
                                                      media_runtime=media_runtime)

    web_runner = None
    if web_port:
        from .webapp import make_web_app
        from aiohttp import web as _web

        def _save_dir() -> str:
            p = store.prefs()
            import os
            d = p.get("saveDir") or os.path.join(
                os.path.expanduser("~"), "Downloads")
            os.makedirs(d, exist_ok=True)
            return d

        def _on_web_text(text: str) -> None:
            from .model import text_bubble
            b = text_bubble("web-visitor", ident["fingerprint"], text)
            store.add_text_bubble(b["id"], b["from"], b["to"], text,
                                  b["time"], "")
            if api_publish:
                api_publish({"type": "bubbleChanged",
                             "payload": {"bubbleId": b["id"], "type": "text"}})

        wapp, _push = make_web_app(store, ident, _on_web_text, _save_dir,
                                   channel_port=device_port,
                                   send_text_fn=send_text)
        wr = _web.AppRunner(wapp)
        await wr.setup()
        await _web.TCPSite(wr, "0.0.0.0", web_port).start()
        web_runner = wr
        pin = store.own_web_pin()
        print(f"[flixd] web send page on http://0.0.0.0:{web_port}  PIN: {pin}",
              file=sys.stderr)
    disc = Discovery(on_device, multicast_if)
    bound = await disc.start()
    if bound:
        print(f"[flixd] discovery multicast via {disc.iface or 'default interface'}",
              file=sys.stderr, flush=True)
    if announce and bound:
        disc.announce(ident, port=device_port)
        await asyncio.sleep(0.5)
        disc.announce(ident, times=3, port=device_port)
    elif announce:
        print("[flixd] discovery unavailable; skipping announce", file=sys.stderr)

    tasks: list[asyncio.Task] = []
    if clip_sync:
        tasks.append(asyncio.create_task(clip_sync_loop(store, ident, clip_state)))
        print("[flixd] clipboard sync enabled for paired devices",
              file=sys.stderr)

    print(f"[flixd] identity {ident['fingerprint']} alias {ident['alias']} "
          f"ip {local_ip()}", file=sys.stderr)
    print(f"[flixd] device channel on 0.0.0.0:{device_port}; "
          f"desktop API on 127.0.0.1:{api_port}", file=sys.stderr)
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        for t in tasks:
            t.cancel()
        await disc.close()
        if web_runner:
            with contextlib.suppress(Exception):
                await web_runner.cleanup()
        with contextlib.suppress(Exception):
            await channel_runner.cleanup()
        with contextlib.suppress(Exception):
            await api_runner.cleanup()
    return 0
