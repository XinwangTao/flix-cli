"""Async runtime shared by flixd and `flix serve`."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import subprocess
import sys
import time
from pathlib import Path

# The daemon runs inside the windowed exe (no console). Windows gives every
# console child (clip.exe) a brand-new visible terminal unless suppressed;
# 0 on non-Windows keeps Popen happy there.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

from . import DEVICE_PORT, is_port_text, local_ip, model
from .adb import AdbClient
from .media_session import SessionManager
from .media_transport import MediaRuntime
from .channel import PeerError, pair_device, ping_v2, post_bubble, post_intent
from .desktop_api import serve_desktop_api
from .discovery import Discovery
from .server import serve_channel


async def set_clipboard_text(text: str) -> None:
    """Best-effort clipboard write; clip.exe understands UTF-16LE with BOM."""
    if not sys.platform.startswith("win"):
        return
    proc = await asyncio.create_subprocess_exec(
        "clip", stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        creationflags=NO_WINDOW)
    data = text.encode("utf-16-le")
    try:
        await asyncio.wait_for(proc.communicate(b"\xff\xfe" + data), timeout=3)
    except (asyncio.TimeoutError, Exception):
        with contextlib.suppress(Exception):
            proc.kill()


def read_clipboard() -> str | None:
    """Local clipboard text via user32 (CF_UNICODETEXT); None if empty/error."""
    if not sys.platform.startswith("win"):
        return None
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    CF_UNICODETEXT = 13
    # Handles/pointers come back as 64-bit values; without explicit restypes
    # ctypes truncates them to 32-bit c_int and reads garbage -- observed as
    # random access violations in wstring_at (0x7F591B90-style addresses).
    u32.GetClipboardData.argtypes = [wintypes.UINT]
    u32.GetClipboardData.restype = wintypes.HANDLE
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalLock.restype = wintypes.LPVOID
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
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
        h = hashlib.sha1(text.encode("utf-8", "replace"),
                         usedforsecurity=False).hexdigest()[:12]
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
            # Only mark as sent after a success: previously last_sent was set
            # before the loop, so a clipboard change made while every peer was
            # offline was silently dropped forever.
            last_sent = text
            print(f"[clip-sync] {h} -> {sent} paired device(s)",
                  file=sys.stderr, flush=True)


async def run_daemon(store, ident, device_port: int, api_port: int,
                     announce: bool = True, clip_sync: bool = True,
                     web_port: int = 0, multicast_if: str | None = None,
                     model_port: int = 0) -> int:
    # Remember the port we actually listen on: a later one-shot CLI send that
    # has to ping_v2 an unknown peer must advertise the real port, or it would
    # teach the official client to write 8891 back over our 18991 record.
    with contextlib.suppress(Exception):
        store.set_pref("devicePort", int(device_port))
    devices: dict[str, dict] = {}
    verify_pending: set[str] = set()

    async def _verify_moved_device(dev: dict) -> None:
        """An announce may not move an already-known peer on its own.

        Announce datagrams are unsigned: anyone can claim a paired
        fingerprint and redirect routing (and outbox retries) to themselves.
        A new address is accepted only after ping_v2 proves the fingerprint
        really answers there.
        """
        fp = dev["fingerprint"]
        ip = dev.get("ip", "")
        port = int(dev.get("port") or DEVICE_PORT)
        try:
            modal = await ping_v2(ip, ident, port=port,
                                  device_port=device_port, timeout=3.0)
        except Exception:
            return
        finally:
            verify_pending.discard(fp)
        if modal.get("fingerprint") != fp:
            print(f"[discover] ignored IP move for {fp[:8]}: identity mismatch",
                  file=sys.stderr, flush=True)
            return
        devices[fp] = dev
        store.upsert_device(fp, alias=dev.get("alias", ""),
                            device_model=dev.get("deviceModel", ""),
                            port=dev.get("port"), version=dev.get("version"),
                            ip=ip, os_name=dev.get("os", ""))
        print(f"[discover] {dev.get('alias')} ({fp[:8]}) moved to "
              f"{ip}:{port}", file=sys.stderr, flush=True)

    def on_device(dev: dict) -> None:
        fp = dev.get("fingerprint")
        if not fp or fp == ident["fingerprint"]:
            return

        def _key(row: dict) -> tuple:
            # `source`/`from` differ between an announce and a scan probe;
            # comparing them caused a redundant upsert and log line on every
            # sweep for a device that had not moved.
            return (row.get("ip"), row.get("port"), row.get("alias"),
                    row.get("deviceModel"), row.get("os"), row.get("version"))

        old_dev = devices.get(fp)
        if old_dev is not None and _key(old_dev) == _key(dev):
            # Still worth a look: a peer that refused the last auto-connect
            # ask (or was offline) only gets retried on the next event for
            # this device, and an unchanged announce IS that event.
            _maybe_auto_connect(dev)
            return
        old_ip = store.device_ip(fp)[0]
        new_ip = dev.get("ip", "")
        if old_ip and new_ip and old_ip != new_ip:
            if fp not in verify_pending:
                verify_pending.add(fp)
                try:
                    asyncio.get_running_loop().create_task(
                        _verify_moved_device(dev))
                except RuntimeError:  # callback outside the loop
                    verify_pending.discard(fp)
                    devices[fp] = dev
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
        _maybe_auto_connect(dev)

    # -- auto-connect ------------------------------------------------------
    # A discovered peer that is not paired yet gets one askPairDevice with
    # extra.auto=true. Both ends of our own build answer it by proving the
    # source identity (ping_v2) and writing the pair, so two machines on the
    # same LAN connect with no click at all. Retries are spaced out: a peer
    # that keeps refusing (auto-connect off there) must not be hammered.
    AUTO_PAIR_RETRY_S = 180.0
    auto_pair_seen: dict[str, float] = {}
    auto_pair_busy: set[str] = set()

    async def _auto_pair(dev: dict) -> None:
        fp = dev.get("fingerprint") or ""
        ip = dev.get("ip") or ""
        if not fp or not ip or store.is_paired(fp):
            return
        port = int(dev.get("port") or DEVICE_PORT)
        auto_pair_busy.add(fp)
        try:
            code, message = await pair_device(ip, ident["fingerprint"], fp, "",
                                              port=port, auto=True)
        except Exception as error:  # noqa: BLE001 - network best effort
            print(f"[pair] auto-connect {fp[:8]} unreachable: {error}",
                  file=sys.stderr, flush=True)
            return
        finally:
            auto_pair_busy.discard(fp)
        if code != 0:
            print(f"[pair] auto-connect {fp[:8]} refused: {message}",
                  file=sys.stderr, flush=True)
            return
        # `{0: success}` only says the peer accepted someone; announces are
        # unsigned, so prove the fingerprint really answers at that address
        # before the pair is written on our side too.
        try:
            modal = await ping_v2(ip, ident, port=port,
                                  device_port=device_port, timeout=3.0)
        except Exception as error:  # noqa: BLE001
            print(f"[pair] auto-connect {fp[:8]} unverifiable: {error}",
                  file=sys.stderr, flush=True)
            return
        if modal.get("fingerprint") != fp:
            print(f"[pair] auto-connect {fp[:8]} identity mismatch at {ip}",
                  file=sys.stderr, flush=True)
            return
        store.add_pair(fp, store.own_pair_key())
        if api_publish:
            api_publish({"type": "pairChanged",
                         "payload": {"fingerprint": fp, "auto": True}})
        print(f"[pair] auto-connected {dev.get('alias')} ({fp[:8]})",
              file=sys.stderr, flush=True)

    def _maybe_auto_connect(dev: dict) -> None:
        fp = dev.get("fingerprint") or ""
        if not fp or not dev.get("ip") or store.is_paired(fp):
            return
        if not store.auto_connect_enabled(fp):
            return
        if fp in auto_pair_busy:
            return
        now = time.monotonic()
        if now - auto_pair_seen.get(fp, 0.0) < AUTO_PAIR_RETRY_S:
            return
        auto_pair_seen[fp] = now
        try:
            asyncio.get_running_loop().create_task(_auto_pair(dev))
        except RuntimeError:  # callback outside the loop (tests/CLI one-shot)
            auto_pair_seen.pop(fp, None)

    def resolve(to: str) -> tuple[str | None, int, str]:
        """fingerprint (exact/prefix) or ip[:port] -> (ip, port, fingerprint)."""
        port = DEVICE_PORT
        target = to
        head, _, tail = to.rpartition(":")
        if is_port_text(tail):
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
        if not (ip and is_port_text(ip.replace(".", ""))):
            return None, p, fp
        return ip, p, fp

    async def send_text(to: str, text: str) -> dict:
        from .outbox import send_text as durable_send
        result = await durable_send(store, ident, to, text,
                                    device_port=device_port)
        bubble = result.pop("bubble", None)
        if bubble and result.get("state") in ("delivered", "queued"):
            # Echo locally so the transcript shows the message either way; the
            # outbox row carries the real delivery state.
            store.add_text_bubble(bubble["id"], bubble["from"], bubble["to"],
                                  bubble["content"], bubble["time"],
                                  bubble.get("groupId") or "")
            if api_publish:
                api_publish({"type": "bubbleChanged",
                             "payload": {"bubbleId": bubble["id"], "type": "text"}})
        return result

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
    async def scan_now() -> list[dict]:
        """Active LAN sweep, then a unicast announce round.

        The sweep finds peers whose port answers (ours by ping_v2, official by
        a bare TCP 8891 connect). The unicast announce then hands our modal to
        every candidate IP, because official clients never answer a probe but
        do parse a valid announce arriving on UDP 8891 — that is what makes
        them show us without restarting.
        """
        from .discovery import local_hosts, register_official, scan_lan
        # Register before the sweep: the official client only needs the
        # ping_v2 modal to move us from 8891 to 18991, and doing it first means
        # the sweep that follows can already see it under its real identity.
        try:
            official = await register_official(ident, device_port)
            if official:
                on_device(official)
        except Exception as exc:  # registration is best effort
            print(f"[scan] official registration failed: {exc}",
                  file=sys.stderr, flush=True)
        hosts = local_hosts(disc.iface)
        found = await scan_lan(
            on_device, ident, device_port,
            hosts=hosts or None,
            ports=tuple(dict.fromkeys((DEVICE_PORT, 18991, device_port))))
        try:
            await asyncio.to_thread(disc.announce_unicast, ident, hosts)
        except Exception as exc:  # announce is best effort, never fatal
            print(f"[scan] unicast announce failed: {exc}",
                  file=sys.stderr, flush=True)
        return found

    api_runner, api_publish = await serve_desktop_api(store, ident, send_text,
                                                      api_port,
                                                      channel_port=device_port,
                                                      webrtc_hub=hub,
                                                      adb_client=adb_client,
                                                      media_manager=media_manager,
                                                      media_runtime=media_runtime,
                                                      scan_fn=scan_now)

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
    model_runner = None
    # The model gateway is a side channel: whatever happens to it (bad config,
    # port taken by a standalone `flix models start`) must never take the
    # device channel and the desktop API down with it.
    if not model_port:
        # The GUI toggle persists "enabled", so a plain restart keeps sharing.
        try:
            from .model_gateway import load_config as _load_model_gw
            _gw = _load_model_gw(store)
            if _gw.get("enabled"):
                model_port = int(_gw["port"])
        except (OSError, ValueError, TypeError) as e:
            print(f"[flixd] model gateway config ignored: {e}",
                  file=sys.stderr, flush=True)
            model_port = 0
    if model_port:
        try:
            from aiohttp import web as _webgw
            from .model_gateway import load_config as _load_model_cfg
            from .model_gateway import make_model_app
            mcfg = _load_model_cfg(store)
            mcfg["port"] = model_port
            mr = _webgw.AppRunner(make_model_app(store, mcfg))
            await mr.setup()
            await _webgw.TCPSite(mr, "0.0.0.0", model_port).start()
            model_runner = mr
            print(f"[flixd] model gateway on http://0.0.0.0:{model_port} -> "
                  f"{mcfg['upstream']}", file=sys.stderr, flush=True)
        except (OSError, ValueError, TypeError) as e:
            print(f"[flixd] model gateway unavailable on port {model_port}: "
                  f"{e} (device channel keeps running)", file=sys.stderr,
                  flush=True)
            model_runner = None
    disc = Discovery(on_device, multicast_if)
    bound = await disc.start()
    if bound:
        print(f"[flixd] discovery multicast via {disc.iface or 'default interface'}",
              file=sys.stderr, flush=True)
    if announce:
        # Sending has its own socket and must not depend on the listen bind:
        # when another process owns UDP 8891 we can still be discovered by
        # phones and official clients, even though we cannot hear announces.
        disc.announce(ident, port=device_port)
        await asyncio.sleep(0.5)
        disc.announce(ident, times=3, port=device_port)
        if not bound:
            print("[flixd] discovery listen unavailable; announcing send-only "
                  "and falling back to active LAN scan", file=sys.stderr,
                  flush=True)

    tasks: list[asyncio.Task] = []
    if clip_sync:
        tasks.append(asyncio.create_task(clip_sync_loop(store, ident, clip_state)))
        print("[flixd] clipboard sync enabled for paired devices",
              file=sys.stderr)

    async def announce_loop() -> None:
        # Official clients only announce at startup; peers that come online
        # later (fresh app installs, replugged machines) would never learn us.
        # A short re-burst every 30s keeps discovery symmetric at zero cost.
        try:
            while True:
                await asyncio.sleep(30)
                await asyncio.to_thread(disc.announce, ident, 2, device_port)
        except asyncio.CancelledError:
            pass

    tasks.append(asyncio.create_task(announce_loop()))
    print("[flixd] periodic announce every 30s", file=sys.stderr, flush=True)

    async def startup_scan() -> None:
        # 常驻扫描：官方客户端只在启动时公告一次，且不回答任何探测；只扫
        # 一次的话，比我们先开机、或中途换网络的设备永远发现不了。每 60 秒
        # 主动扫一遍 /24（TCP 连接 8891），再单播一轮公告，版本无关。
        first = True
        while True:
            try:
                if first:
                    await asyncio.sleep(2)
                    first = False
                else:
                    await asyncio.sleep(60)
                found = await scan_now()
                if found:
                    names = ", ".join(
                        f"{d.get('alias') or d['fingerprint'][:8]}"
                        f"@{d.get('ip')}:{d.get('port')}" for d in found)
                    print(f"[scan] discovered {names}", file=sys.stderr, flush=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[scan] {exc}", file=sys.stderr, flush=True)

    if announce:
        # 主动扫描常驻：每 60 秒一次，配合 /api/discover/scan 手动触发。
        tasks.append(asyncio.create_task(startup_scan()))
        print("[flixd] active LAN scan every 60s; on-demand via /api/discover/scan",
              file=sys.stderr, flush=True)

    async def agent_outbox_loop() -> None:
        # An agent envelope queued while the peer was offline must still land
        # when it comes back; the sender's tool call already returned "queued".
        from .agentlink import flush_outbox
        try:
            while True:
                await asyncio.sleep(30)
                try:
                    result = await flush_outbox(store, ident)
                except Exception as exc:  # never let one bad peer kill the loop
                    print(f"[flixd] agent outbox: {exc}", file=sys.stderr, flush=True)
                    continue
                if result.get("sent"):
                    print(f"[flixd] agent outbox delivered {result['sent']}",
                          file=sys.stderr, flush=True)
        except asyncio.CancelledError:
            pass

    tasks.append(asyncio.create_task(agent_outbox_loop()))

    async def outbox_loop() -> None:
        # Text bubbles are persisted before the first attempt, so a peer that is
        # only briefly offline gets them on a later pass (30 s, TTL 7 days).
        from .outbox import flush as flush_text_outbox
        try:
            while True:
                await asyncio.sleep(30)
                try:
                    result = await flush_text_outbox(store)
                except Exception as exc:  # never let one bad row kill the loop
                    print(f"[flixd] outbox: {exc}", file=sys.stderr, flush=True)
                    continue
                if result.get("sent") or result.get("failed"):
                    print(f"[flixd] outbox delivered {result['sent']} "
                          f"failed {result['failed']} pending {result['pending']}",
                          file=sys.stderr, flush=True)
        except asyncio.CancelledError:
            pass

    tasks.append(asyncio.create_task(outbox_loop()))

    async def presence_probe_loop() -> None:
        # Official clients only announce at startup, so their seenAt would fall
        # outside the 90s presence TTL. Probe each known device with the normal
        # /ping_v2 handshake and refresh seenAt on success. Failures leave the
        # stored timestamp untouched (the device simply reads as offline).
        try:
            while True:
                await asyncio.sleep(3)
                peers = [row for row in store.devices()
                         if row.get("fingerprint") and row.get("ip")
                         and row.get("port")
                         and row.get("fingerprint") != ident["fingerprint"]]
                # Probes used to run serially (3 s each): with ~10 unreachable
                # history rows one pass exceeded the 90 s presence TTL and
                # flipped *online* devices to offline. Bound concurrency so a
                # large table cannot stall the loop or the event loop.
                sem = asyncio.Semaphore(8)

                async def probe(row, sem=sem) -> bool:
                    async with sem:
                        fp = row["fingerprint"]
                        ip = row["ip"]
                        port = int(row["port"])
                        try:
                            modal = await ping_v2(ip, ident, port=port,
                                                  device_port=device_port,
                                                  timeout=3.0)
                        except Exception:
                            return False
                        try:
                            store.upsert_device(
                                fp, alias=modal.get("alias", ""),
                                device_model=modal.get("deviceModel", ""),
                                port=modal.get("port") or port,
                                version=modal.get("version"), ip=ip,
                                os_name=modal.get("os", ""))
                            return True
                        except Exception as e:
                            print(f"[presence] {e}", file=sys.stderr, flush=True)
                            return False

                refreshed = sum(await asyncio.gather(
                    *(probe(row) for row in peers))) if peers else 0
                if refreshed:
                    print(f"[presence] refreshed {refreshed} device(s)",
                          file=sys.stderr, flush=True)
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass

    tasks.append(asyncio.create_task(presence_probe_loop()))
    print("[flixd] presence probe every 60s", file=sys.stderr, flush=True)

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
        if model_runner:
            with contextlib.suppress(Exception):
                await model_runner.cleanup()
        with contextlib.suppress(Exception):
            await channel_runner.cleanup()
        with contextlib.suppress(Exception):
            await api_runner.cleanup()
    return 0
