"""flix CLI — every capability callable from the command line.

Exit codes: 0 ok, 2 usage, 3 network (no device/timeout/unreachable),
4 refused by peer, 5 local environment problem, 130 interrupted.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import DEVICE_PORT, __version__
from .channel import PeerError, post_bubble, post_intent
from .config import Store, load_or_create_identity
from .discovery import Discovery
from .model import clipboard_intent, text_bubble
from .routing import resolve_target


def emit(payload, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    elif isinstance(payload, dict) and ("error" in payload or payload.get("ok") is False):
        print(f"error: {payload.get('error')}", file=sys.stderr)
        for k, v in payload.items():
            if k != "error":
                print(f"  {k}: {v}", file=sys.stderr)
    elif isinstance(payload, list):
        for item in payload:
            print(_fmt_device(item) if "fingerprint" in item else json.dumps(item,
                                                                             ensure_ascii=False))
    elif isinstance(payload, dict):
        for k, v in payload.items():
            print(f"{k}: {v}")
    else:
        print(payload)


def _fmt_device(d: dict) -> str:
    return (f"{d.get('alias') or d.get('fingerprint', '?'):24} "
            f"{str(d.get('fingerprint', ''))[:8]}  "
            f"{d.get('ip', '')}:{d.get('port', '')}  os={d.get('os', '?')} "
            f"v{d.get('version', '?')}")


async def cmd_discover(args) -> int:
    store = Store()
    try:
        ident = load_or_create_identity(store)
        found: dict[str, dict] = {}

        def on_dev(dev: dict) -> None:
            found.setdefault(dev["fingerprint"], dev)

        disc = Discovery(on_dev)
        if not await disc.start():
            emit({"error": "udp-bind-failed", "port": DEVICE_PORT}, args.json)
            return 5
        try:
            if args.announce:
                disc.announce(ident, times=2)
            await asyncio.sleep(args.timeout)
        finally:
            await disc.close()
        devs = [d for fp, d in sorted(found.items()) if fp != ident["fingerprint"]]
        emit([{"fingerprint": d.get("fingerprint"), "alias": d.get("alias"),
               "ip": d.get("ip"), "port": d.get("port"),
               "deviceModel": d.get("deviceModel"), "deviceType": d.get("deviceType"),
               "os": d.get("os"), "version": d.get("version")}
              for d in devs], args.json)
        return 0 if devs else 3
    finally:
        store.close()


async def _send(store, ident, to: str, kind: str, text: str, port_arg) -> int:
    ip, port, fp = resolve_target(store, to)
    if not ip:
        emit({"error": "no-device",
              "detail": f"unknown target {to!r}; run 'flix discover' first"}, False)
        return 3
    try:
        if kind == "text":
            bubble = text_bubble(ident["fingerprint"], fp, text)
            reply = await post_bubble(ip, bubble, port=port or port_arg)
            emit({"ok": True, "id": bubble["id"], "to": fp, "ip": ip,
                  "reply": reply}, True)
        else:
            intent = clipboard_intent(ident["fingerprint"], text)
            reply = await post_intent(ip, intent, port=port or port_arg)
            emit({"ok": True, "to": fp, "ip": ip, "reply": reply}, True)
        return 0
    except PeerError as e:
        code = {"timeout": 3, "unreachable": 3, "rejected": 4}.get(e.kind, 3)
        emit({"error": e.kind, "detail": e.detail, "ip": ip}, True)
        return code


async def cmd_send_text(args) -> int:
    store = Store()
    try:
        return await _send(store, load_or_create_identity(store), args.to,
                           "text", args.text, args.port)
    finally:
        store.close()


async def cmd_clipboard(args) -> int:
    store = Store()
    try:
        text = sys.stdin.read() if args.text == "-" else args.text
        return await _send(store, load_or_create_identity(store), args.to,
                           "clipboard", text, args.port)
    finally:
        store.close()


async def cmd_send_files(args) -> int:
    from .files import send_file
    store = Store()
    try:
        ident = load_or_create_identity(store)
        ip, port, fp = resolve_target(store, args.to)
        if not ip:
            emit({"error": "no-device",
                  "detail": f"unknown target {args.to!r}; run 'flix discover' first"},
                 args.json)
            return 3
        results = []
        for p in args.files:
            results.append(await send_file(ip, p, from_fp=ident["fingerprint"],
                                           to_fp=fp, port=port or args.port))
        ok = bool(results) and all(r.get("ok") for r in results)
        emit({"ok": ok, "to": fp, "results": results}, args.json)
        return 0 if ok else 3
    finally:
        store.close()


async def cmd_probe(args) -> int:
    import aiohttp
    to = args.to
    if to.rpartition(":")[2].isdigit() is False:
        to = f"{to}:{DEVICE_PORT}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{to}/",
                             timeout=aiohttp.ClientTimeout(total=args.timeout)) as r:
                text = (await r.text()).strip()
                is_flix = (r.status == 404 and text == "Route not found")
                emit({"ok": True, "ip": to, "flix": is_flix,
                      "status": r.status, "body": text[:80]}, args.json)
                return 0 if is_flix else 3
    except Exception as e:
        detail = "timeout" if isinstance(e, asyncio.TimeoutError) else str(e)[:120]
        emit({"error": "unreachable", "detail": detail, "ip": to}, args.json)
        return 3


async def cmd_ping(args) -> int:
    """Manual-add handshake: POST /ping_v2, report the peer's deviceModal."""
    from .channel import PeerError, ping_v2
    to = args.to
    if to.rpartition(":")[2].isdigit() is False:
        to = f"{to}:{DEVICE_PORT}"
    ip, _, ps = to.rpartition(":")
    store = Store()
    try:
        ident = load_or_create_identity(store)
        try:
            modal = await ping_v2(ip, ident, port=int(ps) if ps else DEVICE_PORT,
                                  device_port=args.device_port,
                                  timeout=args.timeout)
        except PeerError as e:
            emit({"error": e.kind, "detail": e.detail, "ip": to}, args.json)
            return 3 if e.kind in ("timeout", "unreachable") else 4
        emit({"ok": True, "ip": to, "peer": modal}, args.json)
        return 0
    finally:
        store.close()


def cmd_qrcode(args) -> int:
    """Print the pairing QR payload (flix2:// base64url) for this node."""
    store = Store()
    try:
        ident = load_or_create_identity(store)
        from .qrcode import pair_payload, parse_payload, lan_ips
        payload = pair_payload(ident["fingerprint"], args.device_port,
                               store.own_pair_key(), ips=lan_ips())
        out = {"payload": payload}
        if args.decode:
            out["decoded"] = parse_payload(payload)
        emit(out, args.json)
        return 0
    finally:
        store.close()


def cmd_self(args) -> int:
    store = Store()
    try:
        ident = load_or_create_identity(store)
        emit({"fingerprint": ident["fingerprint"], "alias": ident["alias"],
              "deviceModel": ident.get("deviceModel"), "os": ident.get("os"),
              "version": __version__}, args.json)
    finally:
        store.close()
    return 0


def cmd_devices(args) -> int:
    store = Store()
    try:
        emit(store.devices(), args.json)
    finally:
        store.close()
    return 0


async def cmd_pair(args) -> int:
    """Pair with a device: ping_v2 for identity, then askPairDevice intent."""
    from .channel import PeerError, pair_device, ping_v2
    to = args.to
    if to.rpartition(":")[2].isdigit() is False:
        to = f"{to}:{DEVICE_PORT}"
    ip, _, ps = to.rpartition(":")
    port = int(ps) if ps else DEVICE_PORT
    store = Store()
    try:
        ident = load_or_create_identity(store)
        try:
            modal = await ping_v2(ip, ident, port=port,
                                  device_port=args.device_port)
        except PeerError as e:
            emit({"error": e.kind, "detail": e.detail, "ip": to}, args.json)
            return 3
        peer_fp = modal.get("fingerprint")
        if not peer_fp:
            emit({"error": "protocol", "detail": "peer modal has no fingerprint",
                  "ip": to}, args.json)
            return 4
        store.upsert_device(peer_fp, alias=str(modal.get("alias") or ""),
                            device_model=str(modal.get("deviceModel") or ""),
                            port=modal.get("port"), version=modal.get("version"),
                            ip=ip, os_name=str(modal.get("os") or ""))
        own_key = store.own_pair_key()
        try:
            code, msg = await pair_device(ip, ident["fingerprint"], peer_fp,
                                          args.code or own_key, port=port)
        except PeerError as e:
            emit({"error": e.kind, "detail": e.detail, "ip": to}, args.json)
            return 3 if e.kind in ("timeout", "unreachable") else 4
        ok = code == 0
        if ok:
            store.add_pair(peer_fp, own_key)
        emit({"ok": ok, "paired": ok, "peer": peer_fp, "replyCode": code,
              "detail": msg, "ip": to}, args.json)
        return 0 if ok else 4
    finally:
        store.close()


# --- group chat -------------------------------------------------------------


def cmd_group_create(args) -> int:
    from .groupchat import new_group
    store = Store()
    try:
        g = new_group(store, args.name, args.member or [])
        emit(g, args.json)
        return 0
    finally:
        store.close()


def cmd_group_add(args) -> int:
    store = Store()
    try:
        from .groupchat import find_group
        g = find_group(store, args.group)
        if g is None:
            emit({"error": "no-group", "detail": args.group}, args.json)
            return 2
        store.add_group_member(g["id"], args.fingerprint)
        emit({"ok": True, "group": g["id"],
              "members": store.group_members(g["id"])}, args.json)
        return 0
    finally:
        store.close()


def cmd_group_join(args) -> int:
    """Register a group created on another flixcli node (handshake未解出)."""
    store = Store()
    try:
        import uuid as _uuid
        gid = args.id
        try:
            _uuid.UUID(gid)
        except ValueError:
            emit({"error": "bad-group-id", "detail": "expect a uuid"}, args.json)
            return 2
        store.create_group(gid, args.name)
        ident = load_or_create_identity(store)
        store.add_group_member(gid, ident["fingerprint"])
        if args.member:
            for m in args.member:
                store.add_group_member(gid, m)
        emit({"ok": True, "id": gid, "name": args.name,
              "members": store.group_members(gid)}, args.json)
        return 0
    finally:
        store.close()


def cmd_group_leave(args) -> int:
    store = Store()
    try:
        store.delete_group(args.group)
        emit({"ok": True, "left": args.group}, args.json)
        return 0
    finally:
        store.close()


def cmd_group_list(args) -> int:
    store = Store()
    try:
        emit(store.groups(), args.json)
        return 0
    finally:
        store.close()


async def cmd_group_send(args) -> int:
    from .groupchat import send_group_text
    store = Store()
    try:
        ident = load_or_create_identity(store)
        text = sys.stdin.read() if args.text == "-" else args.text
        result = await send_group_text(store, ident, args.group, text)
        emit(result, args.json)
        return 0 if result.get("ok") else 3
    finally:
        store.close()


def cmd_group_history(args) -> int:
    store = Store()
    try:
        from .groupchat import find_group
        g = find_group(store, args.group)
        if g is None:
            emit({"error": "no-group", "detail": args.group}, args.json)
            return 2
        emit(store.group_history(g["id"], args.limit), args.json)
        return 0
    finally:
        store.close()


def cmd_mcp(args) -> int:
    from .mcp_server import main as mcp_main
    return mcp_main()


async def cmd_serve(args) -> int:
    from .daemon_runtime import run_daemon
    store = Store()
    try:
        ident = load_or_create_identity(store)
        if args.alias:
            ident = {**ident, "alias": args.alias}  # in-memory override only
        return await run_daemon(store, ident, args.device_port, args.api_port,
                                announce=not args.no_announce,
                                clip_sync=not args.no_clip_sync,
                                web_port=args.web_port)
    except OSError as e:
        emit({"error": "env", "detail": f"{e}", "hint": "port in use? choose "
                                                        "--device-port/--api-port"}, False)
        return 5
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="flix", description="Flix-compatible CLI")
    ap.add_argument("--version", action="version", version=f"flix {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, help_, fn, json_default=True):
        p = sub.add_parser(name, help=help_)
        if json_default:
            p.add_argument("--json", action="store_true", help="machine-readable output")
        p.set_defaults(fn=fn)
        return p

    p = add("discover", "listen for device announces on the LAN "
                        "(devices announce at startup)", cmd_discover)
    p.add_argument("--timeout", type=float, default=5.0)
    p.add_argument("--announce", action="store_true",
                   help="also multicast our own announce (2 packets)")

    p = add("send-text", "send a text bubble to a device (ip[:port] or fingerprint)",
            cmd_send_text)
    p.add_argument("to")
    p.add_argument("text")
    p.add_argument("--port", type=int, default=DEVICE_PORT)

    p = add("send", "send file(s) to a device (bubble + POST /file)", cmd_send_files)
    p.add_argument("to")
    p.add_argument("files", nargs="+")
    p.add_argument("--port", type=int, default=DEVICE_PORT)

    p = add("clipboard", "deliver text to a device's clipboard (TransIntent)",
            cmd_clipboard)
    p.add_argument("to")
    p.add_argument("text", nargs="?", default="-",
                   help="text to deliver; '-' reads stdin")
    p.add_argument("--port", type=int, default=DEVICE_PORT)

    p = add("probe", "check whether ip[:port] runs a Flix-compatible channel",
            cmd_probe)
    p.add_argument("to", help="ip[:port] (default port 8891)")
    p.add_argument("--timeout", type=float, default=4.0)

    p = add("qrcode", "print the pairing QR payload (flix2://) for this node",
            cmd_qrcode)
    p.add_argument("--device-port", type=int, default=DEVICE_PORT)
    p.add_argument("--decode", action="store_true",
                   help="also print the decoded fields")

    p = add("ping", "manual-add handshake: POST /ping_v2 and report the "
                    "peer's identity", cmd_ping)
    p.add_argument("to", help="ip[:port] (default port 8891)")
    p.add_argument("--device-port", type=int, default=DEVICE_PORT,
                   help="port advertised in our deviceModal")
    p.add_argument("--timeout", type=float, default=6.0)

    add("self", "print our identity", cmd_self)
    add("devices", "list known devices from local store", cmd_devices)
    p = add("pair", "pair with a device (ping_v2 + askPairDevice intent)",
            cmd_pair)
    p.add_argument("to", help="ip[:port] (default port 8891)")
    p.add_argument("--code", default="",
                   help="peer's 4-digit pairing key (device_pair_key)")
    p.add_argument("--device-port", type=int, default=DEVICE_PORT,
                   help="port advertised in our deviceModal")
    p.add_argument("--timeout", type=float, default=6.0)

    gp = sub.add_parser("group", help="LAN group chat (bubble + groupId, per-member)")
    gsub = gp.add_subparsers(dest="group_cmd", required=True)

    def gadd(name, help_, fn):
        p = gsub.add_parser(name, help=help_)
        p.add_argument("--json", action="store_true")
        p.set_defaults(fn=fn)
        return p

    p = gadd("create", "create a group", cmd_group_create)
    p.add_argument("name")
    p.add_argument("--member", action="append", help="member fingerprint (repeatable)")
    p = gadd("add", "add a member to a group", cmd_group_add)
    p.add_argument("group", help="group id or name")
    p.add_argument("fingerprint")
    p = gadd("join", "register a group created on another node", cmd_group_join)
    p.add_argument("id")
    p.add_argument("name")
    p.add_argument("--member", action="append")
    p = gadd("leave", "remove a group locally", cmd_group_leave)
    p.add_argument("group")
    gadd("list", "list groups", cmd_group_list)
    p = gadd("send", "send text to every member of a group", cmd_group_send)
    p.add_argument("group")
    p.add_argument("text", nargs="?", default="-")
    p = gadd("history", "show recent group bubbles", cmd_group_history)
    p.add_argument("group")
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("mcp", help="run the MCP stdio server (for AI clients)")
    p.set_defaults(fn=cmd_mcp)

    p = sub.add_parser("serve", help="run the resident daemon (same as flixd)")
    p.add_argument("--device-port", type=int, default=DEVICE_PORT)
    p.add_argument("--api-port", type=int, default=8898)
    p.add_argument("--alias", default=None)
    p.add_argument("--no-announce", action="store_true")
    p.add_argument("--no-clip-sync", action="store_true",
                   help="disable auto clipboard sync to paired devices")
    p.add_argument("--web-port", type=int, default=0,
                   help="enable the web send page on this port (e.g. 18992)")
    p.set_defaults(fn=cmd_serve)

    args = ap.parse_args(argv)
    try:
        result = args.fn(args)
        if asyncio.iscoroutine(result):
            result = asyncio.run(result)
        return result
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
