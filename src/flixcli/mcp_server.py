"""Minimal MCP (Model Context Protocol) stdio server for flix capabilities.

Implements the MCP stdio transport: newline-delimited JSON-RPC 2.0 on
stdin/stdout. Methods: initialize, notifications/initialized, tools/list,
tools/call, ping. Tools map 1:1 onto the confirmed CLI capabilities so any
MCP client (AI agent) can drive LAN device communication.

Run:  flix mcp
"""
from __future__ import annotations

import asyncio
import json
import sys

from . import __version__
from .channel import PeerError, post_bubble, post_intent
from .config import Store, load_or_create_identity
from .discovery import Discovery
from .model import clipboard_intent, text_bubble

SERVER_NAME = "flix-mcp"
SERVER_VERSION = __version__

TOOLS = [
    {"name": "flix_self",
     "description": "Get this machine's Flix-compatible identity (fingerprint, alias).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "flix_discover",
     "description": "Discover nearby Flix/flixcli devices on the LAN via UDP multicast "
                    "(listen-only; devices announce at their startup).",
     "inputSchema": {"type": "object",
                     "properties": {"timeout": {"type": "number", "default": 5}},
                     "required": []}},
    {"name": "flix_devices",
     "description": "List known devices from the local store.",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "flix_probe",
     "description": "Check whether ip[:port] runs a Flix-compatible device channel.",
     "inputSchema": {"type": "object",
                     "properties": {"target": {"type": "string"}},
                     "required": ["target"]}},
    {"name": "flix_send_text",
     "description": "Send a text bubble to a device (ip[:port] or fingerprint). "
                    "Works with official Flix clients and other flixcli nodes.",
     "inputSchema": {"type": "object",
                     "properties": {"to": {"type": "string"},
                                    "text": {"type": "string"}},
                     "required": ["to", "text"]}},
    {"name": "flix_send_clipboard",
     "description": "Send clipboard text to a device via TransIntent (action=clipboard); "
                    "the receiver's clipboard is replaced by the text.",
     "inputSchema": {"type": "object",
                     "properties": {"to": {"type": "string"},
                                    "text": {"type": "string"}},
                     "required": ["to", "text"]}},
    {"name": "flix_bubbles",
     "description": "List recent text/file bubbles received by the local flixd.",
     "inputSchema": {"type": "object",
                     "properties": {"limit": {"type": "integer", "default": 20}},
                     "required": []}},
    {"name": "flix_send_files",
     "description": "Send local file(s) to a device (ip[:port] or fingerprint). "
                    "Receiver stores them in its save directory.",
     "inputSchema": {"type": "object",
                     "properties": {"to": {"type": "string"},
                                    "paths": {"type": "array",
                                              "items": {"type": "string"}}},
                     "required": ["to", "paths"]}},
    {"name": "flix_group_list",
     "description": "List LAN groups known to this node (id, name, members).",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "flix_group_send",
     "description": "Send text to every member of a LAN group (bubble + groupId, "
                    "per member). Group must exist locally (flix_group_list).",
     "inputSchema": {"type": "object",
                     "properties": {"group": {"type": "string",
                                              "description": "group id or name"},
                                    "text": {"type": "string"}},
                     "required": ["group", "text"]}},
    {"name": "flix_group_history",
     "description": "Recent messages of a LAN group as received by this node.",
     "inputSchema": {"type": "object",
                     "properties": {"group": {"type": "string"},
                                    "limit": {"type": "integer", "default": 20}},
                     "required": ["group"]}},
    {"name": "flix_pair",
     "description": "Pair with a device (ping_v2 identity exchange then "
                    "askPairDevice). Pairing enables auto file receive on "
                    "official clients.",
     "inputSchema": {"type": "object",
                     "properties": {"to": {"type": "string",
                                           "description": "ip[:port]"},
                                    "code": {"type": "string",
                                             "description": "peer's 4-digit "
                                             "pairing key (optional)"}},
                     "required": ["to"]}},
    {"name": "flix_group_create",
     "description": "Create a LAN group with optional member fingerprints.",
     "inputSchema": {"type": "object",
                     "properties": {"name": {"type": "string"},
                                    "members": {"type": "array",
                                                "items": {"type": "string"}}},
                     "required": ["name"]}},
    {"name": "flix_group_add",
     "description": "Add a member fingerprint to a LAN group (id or name).",
     "inputSchema": {"type": "object",
                     "properties": {"group": {"type": "string"},
                                    "fingerprint": {"type": "string"}},
                     "required": ["group", "fingerprint"]}},
    {"name": "flix_status",
     "description": "Node status: identity, device/group/pairing counts, "
                    "runtime ports — use this first for self-orientation.",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
]


async def _tool_call(name: str, args: dict) -> dict:
    if name == "flix_self":
        store = Store()
        try:
            ident = load_or_create_identity(store)
            return {"fingerprint": ident["fingerprint"], "alias": ident["alias"],
                    "version": __version__}
        finally:
            store.close()
    if name == "flix_discover":
        store = Store()
        try:
            ident = load_or_create_identity(store)
            found: dict[str, dict] = {}

            def on_dev(dev):
                found.setdefault(dev["fingerprint"], dev)

            disc = Discovery(on_dev)
            bound = await disc.start()
            if not bound:
                return {"error": "udp-bind-failed", "port": 8891}
            try:
                await asyncio.sleep(float(args.get("timeout", 5)))
            finally:
                await disc.close()
            return {"devices": [{k: d.get(k) for k in
                                 ("fingerprint", "alias", "ip", "port", "os",
                                  "deviceType")} for d in found.values()]}
        finally:
            store.close()
    if name == "flix_devices":
        store = Store()
        try:
            return {"devices": store.devices()}
        finally:
            store.close()
    if name == "flix_probe":
        import aiohttp
        from . import DEVICE_PORT
        target = str(args.get("target", ""))
        head, _, tail = target.rpartition(":")
        if not tail.isdigit():
            target = f"{target}:{DEVICE_PORT}"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"http://{target}/",
                                 timeout=aiohttp.ClientTimeout(total=4)) as r:
                    text = (await r.text()).strip()
                    ok = r.status == 404 and text == "Route not found"
                    return {"target": target, "flix": ok, "status": r.status}
        except Exception as e:
            return {"target": target, "flix": False, "error": str(e)[:120]}
    if name in ("flix_send_text", "flix_send_clipboard"):
        store = Store()
        try:
            ident = load_or_create_identity(store)
            to = str(args.get("to", ""))
            text = str(args.get("text", ""))
            port = None
            target = to
            head, _, tail = to.rpartition(":")
            if tail.isdigit():
                target, port = head, int(tail)
            ip, fp = None, to
            for row in store.devices():
                if row["fingerprint"].startswith(target) and row.get("ip"):
                    ip, fp = row["ip"], row["fingerprint"]
                    port = port or (row.get("port") or None)
                    break
            if ip is None and target.replace(".", "").isdigit():
                ip = target
            if ip is None:
                return {"error": "no-device", "detail": f"unknown target {to!r}; "
                                                        "run flix_discover first"}
            try:
                if name == "flix_send_text":
                    bubble = text_bubble(ident["fingerprint"], fp, text)
                    reply = await post_bubble(ip, bubble, port=port or 8891)
                    return {"ok": True, "id": bubble["id"], "reply": reply}
                intent = clipboard_intent(ident["fingerprint"], text)
                reply = await post_intent(ip, intent, port=port or 8891)
                return {"ok": True, "reply": reply}
            except PeerError as e:
                return {"ok": False, "error": e.kind, "detail": e.detail}
        finally:
            store.close()
    if name == "flix_pair":
        from .channel import pair_device, ping_v2
        store = Store()
        try:
            ident = load_or_create_identity(store)
            to = str(args.get("to", ""))
            head, _, tail = to.rpartition(":")
            ip, port = (head, int(tail)) if tail.isdigit() else (to, DEVICE_PORT)
            from . import DEVICE_PORT as DP
            port = port or DP
            modal = await ping_v2(ip, ident, port=port)
            peer_fp = modal.get("fingerprint")
            if not peer_fp:
                return {"ok": False, "error": "protocol", "detail": "no fingerprint"}
            store.upsert_device(peer_fp, alias=str(modal.get("alias") or ""),
                                device_model=str(modal.get("deviceModel") or ""),
                                port=modal.get("port"), version=modal.get("version"),
                                ip=ip, os_name=str(modal.get("os") or ""))
            code, msg = await pair_device(ip, ident["fingerprint"], peer_fp,
                                          str(args.get("code") or ""),
                                          port=port)
            if code == 0:
                store.add_pair(peer_fp, store.own_pair_key())
            return {"ok": code == 0, "paired": code == 0, "peer": peer_fp,
                    "replyCode": code, "detail": msg}
        except PeerError as e:
            return {"ok": False, "error": e.kind, "detail": e.detail}
        finally:
            store.close()
    if name == "flix_group_create":
        from .groupchat import new_group
        store = Store()
        try:
            g = new_group(store, str(args.get("name", "")),
                          [str(m) for m in (args.get("members") or [])])
            return {"ok": True, **g}
        finally:
            store.close()
    if name == "flix_group_add":
        from .groupchat import find_group
        store = Store()
        try:
            g = find_group(store, str(args.get("group", "")))
            if not g:
                return {"ok": False, "error": "no-group"}
            fp = str(args.get("fingerprint", ""))
            store.add_group_member(g["id"], fp)
            return {"ok": True, "group": g["id"], "added": fp}
        finally:
            store.close()
    if name == "flix_status":
        store = Store()
        try:
            ident = load_or_create_identity(store)
            return {"fingerprint": ident["fingerprint"], "alias": ident["alias"],
                    "devices": len(store.devices()),
                    "paired": len(store.paired_targets()),
                    "groups": len(store.groups())}
        finally:
            store.close()
    if name == "flix_bubbles":
        store = Store()
        try:
            return {"bubbles": store.bubbles(int(args.get("limit", 20)))}
        finally:
            store.close()
    if name == "flix_send_files":
        from .files import send_file
        from . import DEVICE_PORT
        store = Store()
        try:
            ident = load_or_create_identity(store)
            to = str(args.get("to", ""))
            paths = args.get("paths") or []
            port = None
            target = to
            head, _, tail = to.rpartition(":")
            if tail.isdigit():
                target, port = head, int(tail)
            ip, fp = None, to
            for row in store.devices():
                if row["fingerprint"].startswith(target) and row.get("ip"):
                    ip, fp = row["ip"], row["fingerprint"]
                    port = port or (row.get("port") or None)
                    break
            if ip is None and target.replace(".", "").isdigit():
                ip = target
            if ip is None:
                return {"ok": False, "error": "no-device",
                        "detail": f"unknown target {to!r}"}
            results = [await send_file(ip, str(p), from_fp=ident["fingerprint"],
                                       to_fp=fp, port=port or DEVICE_PORT)
                       for p in paths]
            return {"ok": bool(results) and all(r.get("ok") for r in results),
                    "results": results}
        finally:
            store.close()
    if name == "flix_group_list":
        store = Store()
        try:
            return {"groups": store.groups()}
        finally:
            store.close()
    if name == "flix_group_send":
        from .groupchat import send_group_text
        store = Store()
        try:
            ident = load_or_create_identity(store)
            return await send_group_text(store, ident,
                                         str(args.get("group", "")),
                                         str(args.get("text", "")))
        finally:
            store.close()
    if name == "flix_group_history":
        from .groupchat import find_group
        store = Store()
        try:
            g = find_group(store, str(args.get("group", "")))
            if g is None:
                return {"error": "no-group"}
            return {"group": g["id"], "messages": store.group_history(
                g["id"], int(args.get("limit", 20)))}
        finally:
            store.close()
    return {"error": f"unknown tool {name}"}


def write(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def serve() -> int:
    # Windows-safe: synchronous stdin loop, fresh event loop per tool call.
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            write({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}})
        elif method == "notifications/initialized":
            pass
        elif method == "ping":
            write({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "tools/list":
            write({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name", "")
            args = params.get("arguments") or {}
            try:
                result = asyncio.run(_tool_call(name, args))
                payload = {"content": [{"type": "text",
                                        "text": json.dumps(result, ensure_ascii=False)}]}
                if isinstance(result, dict) and (result.get("ok") is False
                                                 or result.get("error")):
                    payload["isError"] = True
            except Exception as e:  # noqa: BLE001
                payload = {"content": [{"type": "text", "text": f"internal error: {e}"}],
                           "isError": True}
            write({"jsonrpc": "2.0", "id": mid, "result": payload})
        elif mid is not None:
            write({"jsonrpc": "2.0", "id": mid,
                   "error": {"code": -32601, "message": "method not found"}})
    return 0


def main() -> int:
    # stdio transport is UTF-8 per MCP spec; the Windows console default (cp936
    # here) would mojibake Chinese args and emit invalid bytes for U+2014.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    try:
        return serve()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
