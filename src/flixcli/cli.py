"""flix CLI — every capability callable from the command line.

Exit codes: 0 ok, 2 usage, 3 network (no device/timeout/unreachable),
4 refused by peer, 5 local environment problem, 130 interrupted.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request

from . import DEVICE_PORT, __version__, local_ip
from .adb import AdbClient, AdbError
from .channel import PeerError, post_bubble, post_intent
from .config import APP_DIR, Store, load_or_create_identity
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

        disc = Discovery(on_dev, args.multicast_if)
        if not await disc.start():
            emit({"error": "udp-bind-failed", "port": DEVICE_PORT,
                  "interface": disc.iface}, args.json)
            return 5
        print(f"[discover] multicast via {disc.iface or 'default interface'}",
              file=sys.stderr, flush=True)
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


def _tcp_reachable(ip: str, port: int, timeout: float) -> bool:
    import socket
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _firewall_programs() -> tuple[dict, list[str]]:
    """Enabled inbound-allow application filters matching flix/RelayDesk."""
    if not sys.platform.startswith("win"):
        return {}, []
    import subprocess
    script = (
        "$out = @();"
        "Get-NetFirewallApplicationFilter | "
        "Where-Object { $_.Program -match 'flixd|flix-cli|RelayDesk|flix\\.exe' } | "
        "ForEach-Object { $prog = $_.Program;"
        "$rules = Get-NetFirewallRule -AssociatedNetFirewallApplicationFilter $_ "
        "-ErrorAction SilentlyContinue;"
        "if ($rules | Where-Object { $_.Enabled -eq 'True' -and "
        "$_.Direction -eq 'Inbound' -and $_.Action -eq 'Allow' }) "
        "{ $out += $prog } };"
        "$p = Get-NetFirewallProfile | Select-Object Name,Enabled;"
        "@{profiles=@($p); programs=@($out | Select-Object -Unique)} | "
        "ConvertTo-Json -Depth 4 -Compress"
    )
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=12)
        data = json.loads((proc.stdout or "{}").lstrip("﻿"))
    except Exception:
        return {}, []
    rows = data.get("profiles") or []
    if isinstance(rows, dict):
        rows = [rows]
    profiles = {str(row.get("Name")): bool(row.get("Enabled"))
                for row in rows if isinstance(row, dict)}
    programs = data.get("programs") or []
    if isinstance(programs, str):
        programs = [programs]
    return profiles, [str(p) for p in programs if p]


def _multicast_probe(iface: str, group: str) -> dict:
    """Join the group on an ephemeral port so a running flixd keeps :8891."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", 0))
        mreq = (socket.inet_aton(group)
                + socket.inet_aton(iface or "0.0.0.0"))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        return {"ok": True, "probePort": s.getsockname()[1]}
    except OSError as e:
        return {"ok": False, "error": str(e)}
    finally:
        s.close()


def _udp_port_probe(port: int) -> dict:
    """Non-reuse bind: a listener holding the port makes this fail."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("", port))
        return {"port": port, "inUse": False}
    except OSError as e:
        return {"port": port, "inUse": True, "detail": str(e)}
    finally:
        s.close()


async def cmd_netcheck(args) -> int:
    """Read-only LAN diagnosis: interface, multicast join, firewall, peers."""
    from .discovery import MULTICAST_GROUP, default_iface_ip
    from .presence import PRESENCE_TTL_MS

    store = Store()
    try:
        iface = args.multicast_if or default_iface_ip() or ""
        join = {"interface": iface, **_multicast_probe(iface, MULTICAST_GROUP)}
        udp_port = _udp_port_probe(DEVICE_PORT)

        profiles, programs = _firewall_programs()
        exe_norm = sys.executable.lower().replace("/", "\\")
        allowed = any(str(p).lower().replace("/", "\\") == exe_norm
                      for p in programs)
        firewall = {"supported": sys.platform.startswith("win"),
                    "profiles": profiles, "programs": programs,
                    "anyProfileEnabled": any(profiles.values()),
                    "currentExe": sys.executable,
                    "currentExeAllowed": allowed}

        peers = []
        for row in store.devices():
            ip = row.get("ip") or ""
            port = row.get("port")
            if not ip or not port:
                continue
            peers.append({
                "fingerprint": row.get("fingerprint"),
                "alias": row.get("alias"), "ip": ip, "port": port,
                "reachable": await asyncio.to_thread(
                    _tcp_reachable, ip, int(port), args.timeout),
                "seenAt": row.get("seenAt"),
            })

        payload = {"interface": iface, "localIp": local_ip(),
                   "group": f"{MULTICAST_GROUP}:{DEVICE_PORT}",
                   "multicastJoin": join, "udpPort": udp_port,
                   "presenceTtlMs": PRESENCE_TTL_MS,
                   "firewall": firewall, "peers": peers}
        if args.json:
            emit(payload, True)
        else:
            print(f"[netcheck] interface   {iface or 'none'}  "
                  f"local {payload['localIp']}")
            state = "ok" if join["ok"] else "FAILED: " + str(join.get("error", ""))
            print(f"[netcheck] multicast   {payload['group']}  join={state}")
            held = ("held by a process (normal when flixd is running)"
                    if udp_port["inUse"] else "free")
            print(f"[netcheck] udp port    :{DEVICE_PORT} {held}")
            if firewall["supported"]:
                states = ", ".join(f"{k}={v}" for k, v in profiles.items()) or "unknown"
                if profiles and not firewall["anyProfileEnabled"]:
                    print("[netcheck] firewall    all profiles disabled; "
                          "inbound rules not needed")
                else:
                    print(f"[netcheck] firewall    profiles: {states}  "
                          f"current exe allowed: {'yes' if allowed else 'no'}")
            else:
                print("[netcheck] firewall    unsupported on this platform")
            for peer in peers:
                name = peer["alias"] or str(peer["fingerprint"])[:8]
                print(f"[netcheck] peer        {name} "
                      f"{peer['ip']}:{peer['port']}  "
                      f"{'reachable' if peer['reachable'] else 'unreachable'}")
            if not join["ok"]:
                print("[netcheck] hint        multicast join failed; the NIC is "
                      "down, the interface IP is wrong, or a security agent "
                      "blocks multicast")
            elif peers and not any(p["reachable"] for p in peers):
                print("[netcheck] hint        multicast joined but peers are "
                      "unreachable; check the peer firewall "
                      "(scripts/fix_firewall.ps1)")
        return 0 if join["ok"] else 5
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
              "appDir": str(APP_DIR),
              "identitySource": ident.get("adopted_from") or "canonical",
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


def cmd_bubbles(args) -> int:
    store = Store()
    try:
        emit(store.bubbles(args.limit), args.json)
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


def _desktop_api_request(port: int, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            return {"ok": False, "error": f"http-{exc.code}"}
    except (OSError, urllib.error.URLError) as exc:
        return {"ok": False, "error": "daemon-unreachable", "detail": str(exc)}


def cmd_call(args) -> int:
    if args.call_cmd == "list":
        result = _desktop_api_request(args.api_port, "/api/media/sessions")
    elif args.call_cmd == "request":
        capabilities = ["view"]
        if args.audio:
            capabilities.append("audio")
        if args.control:
            capabilities.append("control")
        result = _desktop_api_request(args.api_port, "/api/media/request",
                                       {"deviceId": args.device, "capabilities": capabilities})
    elif args.call_cmd == "approve":
        capabilities = ["view"]
        if args.control:
            capabilities.append("control")
        result = _desktop_api_request(args.api_port, "/api/media/decision",
                                       {"callId": args.call_id, "approve": True,
                                        "capabilities": capabilities})
    elif args.call_cmd == "deny":
        result = _desktop_api_request(args.api_port, "/api/media/decision",
                                       {"callId": args.call_id, "approve": False,
                                        "reason": args.reason})
    elif args.call_cmd == "connect":
        result = _desktop_api_request(args.api_port, "/api/media/offer",
                                       {"callId": args.call_id})
    elif args.call_cmd == "revoke":
        result = _desktop_api_request(args.api_port, "/api/media/revoke",
                                       {"callId": args.call_id, "reason": args.reason})
    else:
        result = {"ok": False, "error": "unknown-call-command"}
    emit(result, args.json)
    return 0 if result.get("ok") else (4 if result.get("error") in
                                        ("not-approved", "not-receiver", "control-not-approved") else 3)


async def cmd_adb(args) -> int:
    client = AdbClient(adb_path=args.adb_path, timeout=args.timeout)
    try:
        if args.adb_cmd == "devices":
            emit({"ok": True, "devices": [d.as_dict() for d in await client.devices()]}, args.json)
            return 0
        if args.adb_cmd == "report":
            emit(await client.report(args.serial), args.json)
            return 0
        if args.adb_cmd == "push":
            result = await client.push(args.source, args.destination, args.serial)
        elif args.adb_cmd == "pull":
            result = await client.pull(args.source, args.destination, args.serial)
        elif args.adb_cmd == "shell":
            result = await client.shell(args.command, args.serial, approved=args.approve)
            emit(result, args.json)
            return 0 if result.get("ok") else (4 if result.get("error") == "approval_required" else 3)
        else:
            emit({"error": "unknown-adb-command"}, args.json)
            return 2
        emit(result, args.json)
        return 0 if result.get("ok") else 3
    except AdbError as e:
        emit({"ok": False, "error": e.kind, "detail": e.detail,
              "returncode": e.returncode}, args.json)
        return 3


def cmd_gui(args) -> int:
    try:
        from .gui import main as gui_main
    except ImportError as exc:
        emit({"error": "gui-dependency", "detail": str(exc),
              "hint": "install with: pip install -e .[gui]"}, args.json)
        return 5
    return gui_main([], api_port=args.api_port)


def cmd_agent(args) -> int:
    from .agent import main as agent_main
    return agent_main(args)


def cmd_ui(args) -> int:
    from . import ui
    return ui.main(args)


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
                                web_port=args.web_port,
                                multicast_if=args.multicast_if)
    except OSError as e:
        emit({"error": "env", "detail": f"{e}", "hint": "port in use? choose "
                                                        "--device-port/--api-port"}, False)
        return 5
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy codepage (cp936 here); Chinese
    # payloads would raise UnicodeEncodeError. Keep bytes UTF-8 for pipes and
    # replace unrepresentable chars on a real console instead of crashing.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
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
    p.add_argument("--multicast-if", default=None, metavar="IP",
                   help="interface IP for discovery multicast (default: auto)")

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

    p = add("netcheck", "read-only LAN diagnosis: interface, multicast join, "
                      "firewall rules and peer reachability", cmd_netcheck)
    p.add_argument("--multicast-if", default=None,
                   help="pin the discovery NIC by local IP (default: auto)")
    p.add_argument("--timeout", type=float, default=2.0,
                   help="TCP connect timeout per peer in seconds")

    add("self", "print our identity", cmd_self)
    add("devices", "list known devices from local store", cmd_devices)
    p = add("bubbles", "recent text/file bubbles received by this node",
            cmd_bubbles)
    p.add_argument("--limit", type=int, default=20)
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

    p = sub.add_parser("ui", help="native desktop window for group chats")
    p.add_argument("--group", default=None, help="group name to open")
    p.set_defaults(fn=cmd_ui)
    p = sub.add_parser("call", help="request and manage an approved remote media session")
    p.add_argument("--json", action="store_true")
    p.add_argument("--api-port", type=int, default=8898)
    csub = p.add_subparsers(dest="call_cmd", required=True)
    csub.add_parser("list", help="list media sessions")
    p_req = csub.add_parser("request", help="request a peer session")
    p_req.add_argument("device")
    p_req.add_argument("--audio", action="store_true")
    p_req.add_argument("--control", action="store_true")
    p_app = csub.add_parser("approve", help="approve a pending session locally")
    p_app.add_argument("call_id")
    p_app.add_argument("--control", action="store_true")
    p_den = csub.add_parser("deny", help="deny a pending session")
    p_den.add_argument("call_id")
    p_den.add_argument("--reason", default="denied by user")
    p_con = csub.add_parser("connect", help="connect an approved session")
    p_con.add_argument("call_id")
    p_rev = csub.add_parser("revoke", help="revoke a media session")
    p_rev.add_argument("call_id")
    p_rev.add_argument("--reason", default="revoked by user")
    p.set_defaults(fn=cmd_call)

    p = sub.add_parser("adb", help="inspect and transfer files with an authorized Android device")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--serial", default=None, help="ADB serial; required when ambiguous")
    p.add_argument("--adb-path", default=None, help="adb executable path")
    p.add_argument("--timeout", type=float, default=30.0)
    adb_sub = p.add_subparsers(dest="adb_cmd", required=True)
    adb_sub.add_parser("devices", help="list connected devices")
    adb_sub.add_parser("report", help="show device authorization report")
    p_push = adb_sub.add_parser("push", help="copy a local file to Android")
    p_push.add_argument("source")
    p_push.add_argument("destination")
    p_pull = adb_sub.add_parser("pull", help="copy an Android file to this machine")
    p_pull.add_argument("source")
    p_pull.add_argument("destination")
    p_shell = adb_sub.add_parser("shell", help="request a shell command; approval is required")
    # REMAINDER keeps leading-dash command args (e.g. am start -n) intact
    # instead of argparse eating them as flix options. --approve must come
    # before the command words.
    p_shell.add_argument("command", nargs=argparse.REMAINDER)
    p_shell.add_argument("--approve", action="store_true",
                         help="explicitly approve this local operator action")
    p.set_defaults(fn=cmd_adb)

    p = sub.add_parser("gui", help="open the Windows desktop control surface")
    p.add_argument("--json", action="store_true", help="machine-readable errors")
    p.add_argument("--api-port", type=int, default=8898,
                   help="flixd Desktop API port (default: 8898)")
    p.set_defaults(fn=cmd_gui)

    p = sub.add_parser("mcp", help="run the MCP stdio server (for AI clients)")
    p.set_defaults(fn=cmd_mcp)

    p = sub.add_parser("agent", help="auto-answer incoming bubbles with a command")
    p.add_argument("--cmd", required=True,
                   help="run per message: JSON on stdin, reply text on stdout")
    p.add_argument("--interval", type=float, default=2.0,
                   help="poll interval in seconds")
    p.add_argument("--prefix", default="[A-AI]",
                   help="our reply prefix; messages starting with it are ignored")
    p.add_argument("--min-gap", type=float, default=30.0,
                   help="seconds between replies to the same peer")
    p.add_argument("--max-hop", type=int, default=2,
                   help="stop replying once the hop counter reaches this")
    p.add_argument("--timeout", type=float, default=60.0,
                   help="per-command timeout in seconds")
    p.add_argument("--once", action="store_true", help="one pass, then exit")
    p.add_argument("--dry-run", action="store_true",
                   help="log the reply instead of sending it")
    p.set_defaults(fn=cmd_agent)

    p = sub.add_parser("serve", help="run the resident daemon (same as flixd)")
    p.add_argument("--device-port", type=int, default=DEVICE_PORT)
    p.add_argument("--api-port", type=int, default=8898)
    p.add_argument("--alias", default=None)
    p.add_argument("--no-announce", action="store_true")
    p.add_argument("--no-clip-sync", action="store_true",
                   help="disable auto clipboard sync to paired devices")
    p.add_argument("--web-port", type=int, default=0,
                   help="enable the web send page on this port (e.g. 18992)")
    p.add_argument("--multicast-if", default=None, metavar="IP",
                   help="interface IP for discovery multicast (default: auto)")
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
