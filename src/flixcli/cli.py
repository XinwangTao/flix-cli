"""flix CLI — every capability callable from the command line.

Exit codes: 0 ok, 2 usage, 3 network (no device/timeout/unreachable),
4 refused by peer, 5 local environment problem, 130 interrupted.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import (DEVICE_PORT, __version__, is_port_text, local_ip,
                   virtual_interfaces)
from .adb import AdbClient, AdbError
from .channel import PeerError, post_bubble, post_intent, post_json
from .config import APP_DIR, Store, load_or_create_identity
from .discovery import Discovery
from .model import clipboard_intent, shell_intent, text_bubble
from .routing import resolve_target


def _join_remote_command(parts: list[str]) -> str:
    """Rebuild one command line without losing the caller's argument quoting.

    `" ".join(parts)` turned `python -c "print('a b')"` into
    `python -c print('a b')` on the peer. Quote for the platform the command
    will run on (both flixcli ends are the same OS in practice).
    """
    if not parts:
        return ""
    if sys.platform == "win32":
        import subprocess
        return subprocess.list2cmdline(parts)
    import shlex
    return shlex.join(parts)


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
    from .outbox import send_text as durable_send
    store = Store()
    try:
        result = await durable_send(store, load_or_create_identity(store),
                                    args.to, args.text, args.port)
    finally:
        store.close()
    emit(result, args.json)
    return 0 if result.get("ok") else 3


def _sibling_device_port(port: int) -> int | None:
    """RelayDesk 的两个常规设备端口：8891（首选）与 18991（8891 被占用时）。"""
    if port == DEVICE_PORT:
        return 18991
    if port == 18991:
        return DEVICE_PORT
    return None


async def cmd_shell(args) -> int:
    """Run one command on a paired peer (exec only, no PTY, no streaming).

    Exit code is the remote command's exit code (like ssh); transport and
    trust failures keep the CLI's own codes: 3 network, 4 refused.
    """
    parts = list(args.command)
    if parts and parts[0] == "--":
        parts = parts[1:]
    command = _join_remote_command(parts)
    if not command and not getattr(args, "pty", False):
        emit({"error": "usage",
              "detail": "nothing to run; use: flix shell <device> -- <command>"}, True)
        return 2
    store = Store()
    try:
        ident = load_or_create_identity(store)
        ip, port, fp = resolve_target(store, args.to)
        if not ip:
            emit({"error": "no-device", "detail": f"unknown target {args.to!r}"}, True)
            return 3
        if not store.is_paired(fp):
            emit({"error": "not-paired",
                  "detail": f"{fp} is not paired; run 'flix pair' first"}, True)
            return 4
        if getattr(args, "pty", False):
            return await _pty_client(ip, port or args.port,
                                     ident["fingerprint"], args.shell)
        intent = shell_intent(ident["fingerprint"], fp, uuid.uuid4().hex,
                              command, args.timeout)
        # GUI 启动 `serve` 时优先用 8891，只有 8891 被占用才退回 18991
        # （见 gui.py 的 device_port 选择）。所以「另一台机器到底开在哪个端口」
        # 是随机器而定的：同一个人在两台机器上敲同一条命令，可能一个通一个不通。
        # 这里在连不上时自动再试另一个候选端口，并把实际用到的端口告诉用户。
        candidates = [port or args.port]
        sibling = _sibling_device_port(candidates[0])
        if sibling:
            candidates.append(sibling)
        result = None
        last_error = None
        used_port = candidates[0]
        for index, candidate_port in enumerate(candidates):
            try:
                result = await post_json(ip, "/intent", intent,
                                         port=candidate_port,
                                         timeout=args.timeout + 15)
                used_port = candidate_port
                if index:
                    emit({"note": "device-port-fallback",
                          "detail": f"端口 {candidates[0]} 连不上，已自动改用 "
                                    f"{candidate_port}", "to": fp}, True)
                break
            except PeerError as e:
                last_error = e
                if e.kind not in ("timeout", "unreachable") or index + 1 == len(candidates):
                    break
                continue
        if result is None:
            e = last_error
            code = {"timeout": 3, "unreachable": 3, "rejected": 4}.get(e.kind, 3)
            emit({"error": e.kind, "detail": e.detail, "to": fp, "ip": ip,
                  "tried_ports": candidates}, True)
            return code
    finally:
        store.close()
    if args.json:
        emit({"ok": True, "to": fp, "ip": ip, "port": used_port, **result}, True)
    else:
        out = result.get("stdout") or ""
        err = result.get("stderr") or ""
        if out:
            sys.stdout.write(out if out.endswith("\n") else out + "\n")
        if err:
            sys.stderr.write(err if err.endswith("\n") else err + "\n")
        if result.get("truncated"):
            print("[flix] output truncated at 256KB", file=sys.stderr)
        if result.get("timeout"):
            print(f"[flix] timed out after {args.timeout:g}s; process tree killed",
                  file=sys.stderr)
    return int(result.get("exit") or 0)


def _term_cols_rows() -> tuple[int, int]:
    import shutil
    size = shutil.get_terminal_size((80, 24))
    return max(size.columns, 20), max(size.lines, 5)


class _ConsoleRaw:
    """Local console in raw mode, restored on exit."""

    def __init__(self):
        self._saved = None
        self._saved_out = None

    def __enter__(self):
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32
            hin = k32.GetStdHandle(-10)
            mode = wintypes.DWORD()
            if k32.GetConsoleMode(hin, ctypes.byref(mode)):
                self._saved = (hin, mode.value)
                k32.SetConsoleMode(hin, mode.value & ~0x0006)  # no line input/echo
            hout = k32.GetStdHandle(-11)
            omode = wintypes.DWORD()
            if k32.GetConsoleMode(hout, ctypes.byref(omode)):
                self._saved_out = (hout, omode.value)
                k32.SetConsoleMode(hout, omode.value | 0x0004)  # VT processing
        else:
            import termios
            import tty
            fd = sys.stdin.fileno()
            self._saved = (fd, termios.tcgetattr(fd))
            tty.setraw(fd)
        return self

    def __exit__(self, *_exc):
        if os.name == "nt":
            import ctypes
            k32 = ctypes.windll.kernel32
            for saved in (self._saved, self._saved_out):
                if saved:
                    with contextlib.suppress(Exception):
                        k32.SetConsoleMode(*saved)
        else:
            import termios
            fd, attrs = self._saved
            termios.tcsetattr(fd, termios.TCSADRAIN, attrs)


_WIN_KEYS = {"H": b"\x1b[A", "P": b"\x1b[B", "M": b"\x1b[C", "K": b"\x1b[D",
             "G": b"\x1b[H", "O": b"\x1b[F", "R": b"\x1b[3~", "S": b"\x7f",
             "I": b"\x1b[5~", "Q": b"\x1b[6~"}


def _read_keys() -> bytes | None:
    """One chunk of local keystrokes; `None` means stdin reached EOF.

    Scripted callers pipe stdin, and a closed pipe has to end the session —
    otherwise the client sits in raw mode forever waiting for keys nobody will
    type (and the peer keeps its single PTY slot allocated).
    """
    if not sys.stdin.isatty():
        try:
            return sys.stdin.buffer.read1(1024) or None
        except (OSError, ValueError):
            return None
    if os.name == "nt":
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return _WIN_KEYS.get(msvcrt.getwch(), b"")
        if ch == "\r":
            return b"\r"
        if ch == "\x08":
            return b"\x7f"
        return ch.encode("utf-8", "replace")
    return os.read(sys.stdin.fileno(), 1024)


async def _pty_client(ip: str, port: int, my_fp: str, shell_name: str) -> int:
    """Interactive terminal: local raw console <-> remote ConPTY over WebSocket."""
    import aiohttp

    cols, rows = _term_cols_rows()
    query = urllib.parse.urlencode({"from": my_fp, "sid": uuid.uuid4().hex,
                                    "cols": cols, "rows": rows,
                                    "shell": shell_name})
    url = f"http://{ip}:{port}/shell/pty?{query}"
    loop = asyncio.get_running_loop()
    with _ConsoleRaw():
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(url, timeout=20,
                                              max_msg_size=8 * 1024 * 1024) as ws:
                    stop = asyncio.Event()

                    async def keyboard() -> None:
                        while not stop.is_set():
                            data = await loop.run_in_executor(None, _read_keys)
                            if data is None:  # stdin closed: ask the peer to end
                                with contextlib.suppress(Exception):
                                    await ws.send_json({"t": "close"})
                                return
                            if not data:
                                await asyncio.sleep(0.02)
                                continue
                            if b"\x1d" in data:  # Ctrl+] 本地退出
                                await ws.send_json({"t": "close"})
                                return
                            await ws.send_json(
                                {"t": "data",
                                 "b": base64.b64encode(data).decode()})

                    async def resize_watch() -> None:
                        last = (cols, rows)
                        while not stop.is_set():
                            await asyncio.sleep(1.0)
                            cur = _term_cols_rows()
                            if cur != last:
                                last = cur
                                await ws.send_json(
                                    {"t": "resize", "cols": cur[0], "rows": cur[1]})

                    tasks = [asyncio.create_task(keyboard()),
                             asyncio.create_task(resize_watch())]
                    try:
                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                continue
                            try:
                                frame = json.loads(msg.data)
                            except ValueError:
                                continue
                            kind = frame.get("t")
                            if kind == "data":
                                sys.stdout.buffer.write(
                                    base64.b64decode(frame.get("b") or ""))
                                sys.stdout.buffer.flush()
                            elif kind == "exit":
                                code = frame.get("code")
                                note = f" exit={code}" if code is not None else ""
                                print(f"\r\n[flix] 会话结束{note}")
                                return int(code or 0)
                            elif kind == "error":
                                print(f"\r\n[flix] {frame.get('error')}: "
                                      f"{frame.get('detail', '')}", file=sys.stderr)
                                return 5
                    finally:
                        stop.set()
                        for task in tasks:
                            task.cancel()
                    print("\r\n[flix] 会话已关闭")
                    return 0
        except aiohttp.WSServerHandshakeError as exc:
            # The peer answers before upgrading, so this is the only chance to
            # tell the operator *why* the terminal did not open.
            hint = {
                409: "对端 PTY 槽位已被占用（一个活动会话，或旧版本的泄漏）"
                     " → 到对端重启 RelayDesk",
                403: "对端拒绝：未配对，或来源 IP 与配对记录不一致",
                503: "对端缺 pywinpty，无法分配伪终端 → 改用 "
                     "flix shell <设备> -- <命令>",
            }.get(exc.status)
            print(f"[flix] 握手失败 HTTP {exc.status}" +
                  (f"：{hint}" if hint else ""), file=sys.stderr)
            return 5
        except aiohttp.ClientError as exc:
            print(f"[flix] 连接失败: {exc}", file=sys.stderr)
            return 3


async def cmd_clipboard(args) -> int:
    store = Store()
    try:
        text = sys.stdin.read() if args.text == "-" else args.text
        return await _send(store, load_or_create_identity(store), args.to,
                           "clipboard", text, args.port)
    finally:
        store.close()


async def cmd_upgrade(args) -> int:
    """Ship a new exe to a paired device, restart it, and verify it came up."""
    from .upgrade import upgrade
    store = Store()
    try:
        ident = load_or_create_identity(store)
        try:
            result = await upgrade(store, ident, args.to, args.file,
                                   wait_s=args.wait, port_arg=args.port,
                                   fresh_env=args.fresh_env)
        except ValueError as exc:
            emit({"ok": False, "error": "bad-package", "detail": str(exc)}, True)
            return 2
    finally:
        store.close()
    emit(result, args.json)
    if result.get("ok"):
        return 0
    return 4 if result.get("error") == "not-paired" else 3


def cmd_update(args) -> int:
    """Install a newer build from the GitHub release page.

    Exit codes: 10 means an update exists but `--check` was asked for, so the
    caller can tell "nothing to do" from "act now".
    """
    from . import autoupdate as au

    def emit_safely(payload: dict) -> None:
        # The GUI that launched us may have been stopped to swap its own exe;
        # its stdout pipe is then closed and must not crash the updater.
        try:
            emit(payload, args.json)
        except (OSError, ValueError):
            pass

    mirrors = tuple(args.mirror) if args.mirror else au.MIRRORS
    state_dir = Path(args.state_dir) if args.state_dir else None
    log_path = (state_dir / "update.log") if state_dir else au.LOG_PATH
    try:
        result = au.run_update(
            repo=args.repo, api_base=args.api_base,
            token=args.token or os.environ.get("GITHUB_TOKEN"),
            mirror_list=mirrors, exe=args.exe, check_only=args.check,
            force=args.force, dry_run=args.dry_run,
            restart=not args.no_restart, start_when_idle=args.start,
            include_prerelease=args.pre, asset_pattern=args.asset,
            wait_s=args.wait, installed_version=args.installed_version,
            state_path=(state_dir / "update_state.json") if state_dir else au.STATE_PATH,
            log_path=log_path,
            downloads_dir=args.downloads_dir, echo=not args.quiet)
    except au.UpdateError as exc:
        # A headless run has no console: leave the reason in the log too.
        au.log_line(f"failed {exc.kind}: {exc.detail}", path=log_path)
        if not args.quiet:
            emit_safely({"ok": False, "error": exc.kind, "detail": exc.detail})
        return exc.code
    if not args.quiet:
        emit_safely(result)
    if not result.get("ok"):
        return au.EXIT_INSTALL
    if args.check and result.get("status") == "update-available":
        return au.EXIT_UPDATE_AVAILABLE
    return 0


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
    if not is_port_text(to.rpartition(":")[2]):
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
    if not is_port_text(to.rpartition(":")[2]):
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


def _firewall_coverage(app_profiles: list[str], categories: list[str]) -> dict:
    """Which of the *current* network categories a set of allow rules covers.

    A rule that exists but only covers `Public` does nothing on a Wi-Fi that
    Windows classified as `Private` — `netcheck` used to say "allowed: yes"
    there and the peer still could not connect. Rule existence is not enough;
    the profile has to match the network the peers are on.
    """
    covered: set[str] = set()
    for raw in app_profiles:
        parts = {p.strip().lower() for p in str(raw).replace(",", " ").split()}
        if "any" in parts:
            covered.update(c.lower() for c in categories)
            continue
        for category in categories:
            if category.lower() in parts:
                covered.add(category.lower())
    missing = [c for c in categories if c.lower() not in covered]
    return {"covered": sorted(covered), "uncovered": sorted(missing)}


def _firewall_programs() -> dict:
    """Inbound-allow rules for flix/RelayDesk, with the profiles each one covers.

    Enumerating *all* firewall rules takes minutes on a busy host (thousands of
    entries) and used to time out, which made `netcheck` report "unknown". The
    rules that matter are named after the product, so query them by name — a
    3 s round trip — and resolve program/port only for the handful that match.
    """
    empty = {"profiles": {}, "programs": [], "apps": [], "categories": []}
    if not sys.platform.startswith("win"):
        return empty
    import subprocess
    script = (
        "$out = @();"
        "Get-NetFirewallRule -DisplayName '*flix*','*RelayDesk*' "
        "-ErrorAction SilentlyContinue | "
        "Where-Object { $_.Direction -eq 'Inbound' -and $_.Enabled -eq 'True' "
        "-and $_.Action -eq 'Allow' } | ForEach-Object {"
        "$app = $_ | Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue;"
        "$port = $_ | Get-NetFirewallPortFilter -ErrorAction SilentlyContinue;"
        "$out += @{ name = [string]$_.DisplayName;"
        "profile = [string]$_.Profile;"
        "program = [string]$app.Program;"
        "port = [string]$port.LocalPort } };"
        "$cats = @(Get-NetConnectionProfile -ErrorAction SilentlyContinue |"
        "ForEach-Object { [string]$_.NetworkCategory } | Select-Object -Unique);"
        "$p = @(Get-NetFirewallProfile -ErrorAction SilentlyContinue |"
        "ForEach-Object { @{ Name = [string]$_.Name; Enabled = [string]$_.Enabled } });"
        "@{profiles=@($p); apps=@($out); categories=@($cats)} | "
        "ConvertTo-Json -Depth 5 -Compress"
    )
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=45)
        data = json.loads((proc.stdout or "{}").lstrip("﻿"))
    except Exception:
        return empty
    rows = data.get("profiles") or []
    if isinstance(rows, dict):
        rows = [rows]
    profiles = {str(row.get("Name")): str(row.get("Enabled")).lower() == "true"
                for row in rows if isinstance(row, dict)}
    apps = [row for row in (data.get("apps") or []) if isinstance(row, dict)]
    programs = sorted({str(row.get("program")) for row in apps
                       if row.get("program")})
    categories = [str(c) for c in (data.get("categories") or []) if str(c)]
    return {"profiles": profiles, "programs": programs, "apps": apps,
            "categories": categories}


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

        fw = _firewall_programs()
        profiles = fw["profiles"]
        programs = fw["programs"]
        exe_norm = sys.executable.lower().replace("/", "\\")
        allowed = any(str(p).lower().replace("/", "\\") == exe_norm
                      for p in programs)
        # Rule existence is not the same as coverage: a Public-only rule is
        # useless on the Private Wi-Fi that phones and peers actually use.
        ours = [row.get("profile", "") for row in fw["apps"]
                if str(row.get("program", "")).lower().replace("/", "\\")
                in {str(p).lower().replace("/", "\\") for p in programs}]
        coverage = _firewall_coverage(ours, fw["categories"])
        firewall = {"supported": sys.platform.startswith("win"),
                    "profiles": profiles, "programs": programs,
                    "anyProfileEnabled": any(profiles.values()),
                    "currentExe": sys.executable,
                    "currentExeAllowed": allowed,
                    "ruleProfiles": ours,
                    "networkCategories": fw["categories"],
                    "coveredCategories": coverage["covered"],
                    "uncoveredCategories": coverage["uncovered"]}

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

        # Name the adapters we refuse to announce on: on a host with VMware or
        # a VPN client the picked interface often looks wrong until the user
        # sees which one was skipped and why.
        excluded = [{"ip": ip, "adapter": name}
                    for ip, name in virtual_interfaces() if ip != iface]
        payload = {"interface": iface, "localIp": local_ip(),
                   "group": f"{MULTICAST_GROUP}:{DEVICE_PORT}",
                   "multicastJoin": join, "udpPort": udp_port,
                   "presenceTtlMs": PRESENCE_TTL_MS,
                   "excludedInterfaces": excluded,
                   "firewall": firewall, "peers": peers}
        if args.json:
            emit(payload, True)
        else:
            print(f"[netcheck] interface   {iface or 'none'}  "
                  f"local {payload['localIp']}")
            for row in excluded[:4]:
                print(f"[netcheck] skipped     {row['ip']}  "
                      f"{row['adapter'] or 'virtual adapter'}")
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
                    cats = ", ".join(fw["categories"]) or "unknown"
                    print(f"[netcheck] firewall    network categories: {cats}  "
                          f"rule profiles: {', '.join(ours) or 'none'}")
                    if firewall["uncoveredCategories"]:
                        need = firewall["uncoveredCategories"][0]
                        print(f"[netcheck] WARNING     inbound rules do not cover "
                              f"{', '.join(firewall['uncoveredCategories'])} — "
                              f"peers on this network cannot connect")
                        print("[netcheck] hint        fix once in an elevated "
                              "PowerShell: "
                              f"scripts\\fix_firewall.ps1 -Profile {need}")
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
    """Print the pairing QR payload for this node.

    Two formats: `flix2://` (base64, CLI peers) and `--link`/`--with-model`
    (`relaydesk://pair?...`, the only form the Android app parses). With
    `--with-model` the same code also carries the LAN model-gateway base URL
    and token, so one scan pairs *and* configures model sharing.
    """
    store = Store()
    try:
        ident = load_or_create_identity(store)
        from .qrcode import pair_payload, parse_payload, lan_ips, pair_uri
        code = store.own_pair_key()
        ips = lan_ips()
        payload = pair_payload(ident["fingerprint"], args.device_port, code, ips=ips)
        out = {"payload": payload}
        if args.decode:
            out["decoded"] = parse_payload(payload)
        if args.link or args.with_model:
            model = None
            if args.with_model:
                from .model_gateway import share_info
                model = share_info(store)
                if model and model.get("listening"):
                    out["note"] = "扫码即配对并写入模型网关地址与令牌"
                elif model:
                    out["note"] = ("模型共享已开启但未在监听：二维码仍带上地址与令牌，"
                                   "请先在本机启动模型共享再让手机使用")
                else:
                    out["note"] = ("模型共享未开启：二维码只含配对信息；"
                                   "先运行 flix models start 再生成")
            out["link"] = pair_uri(ident["fingerprint"], args.device_port, code,
                                   ips=ips,
                                   model_base=(model or {}).get("base", ""),
                                   model_token=(model or {}).get("token", ""))
            if model:
                out["model"] = model
                out["warning"] = ("二维码含模型令牌，等同密码：勿截图转发；"
                                  "泄露后运行 flix models token --reset")
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
        emit(store.visible_devices(), args.json)
    finally:
        store.close()
    return 0


def cmd_outbox(args) -> int:
    """Inspect or fix the durable delivery queue."""
    store = Store()
    try:
        if args.drop:
            ok = store.outbox_drop(args.drop)
            emit({"ok": ok, "dropped": args.drop}, args.json)
            return 0 if ok else 2
        if args.retry:
            ok = store.outbox_retry(args.retry)
            emit({"ok": ok, "queued": args.retry}, args.json)
            return 0 if ok else 2
        rows = store.outbox_list(args.limit)
        if args.json:
            emit(rows, True)
            return 0
        if not rows:
            print("outbox 为空")
        for row in rows:
            print(f"{row['state']:<9} {row['id'][:8]} {row['peer'][:8]} "
                  f"try={row['attempts']} {row['lastError']}")
        return 0
    finally:
        store.close()


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
    # Fail fast: the receiver verifies verifyCode, so a code-less attempt can
    # never succeed and should not even touch the network.
    if not args.code:
        emit({"error": "pair-code-required",
              "detail": "pass --code <peer 4-digit key>; run `flix qrcode` "
                        "on the peer or scan its pairing QR",
              "target": args.to}, args.json)
        return 2
    to = args.to
    if not is_port_text(to.rpartition(":")[2]):
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
                                          args.code, port=port)
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


async def cmd_group_create(args) -> int:
    from .groupchat import new_group, update_group
    store = Store()
    try:
        ident = load_or_create_identity(store)
        g = new_group(store, args.name, args.member or [])
        # Creating a group must tell its members, otherwise the peer keeps
        # receiving bubbles with a groupId it has no row for.
        res = await update_group(store, ident, g["id"])
        emit({**g, "delivered": res.get("delivered", 0),
              "total": res.get("total", 0),
              "results": res.get("results", [])}, args.json)
        return 0
    finally:
        store.close()


async def cmd_group_add(args) -> int:
    from .groupchat import update_group
    store = Store()
    try:
        ident = load_or_create_identity(store)
        res = await update_group(store, ident, args.group,
                                 add=(args.fingerprint,))
        if not res.get("ok"):
            emit(res, args.json)
            return 2
        emit({"ok": True, "group": res["group"], "name": res.get("name", ""),
              "members": res.get("members", []),
              "delivered": res.get("delivered", 0),
              "total": res.get("total", 0),
              "results": res.get("results", [])}, args.json)
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
        with urllib.request.urlopen(req, timeout=10) as response:  # nosec B310
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
        if args.adb_cmd == "pair-qr":
            from .adb_wireless import (DEFAULT_RELAY_PORT, AdbWirelessError,
                                       connect as wireless_connect,
                                       pair as wireless_pair, pairing_payload)
            payload, service, password = pairing_payload()
            emit({"ok": True, "qr": payload, "service": service,
                  "hint": "手机：无线调试 → 使用二维码配对设备 → 扫这张码"},
                 args.json)
            if not args.json:
                print(payload)
            try:
                paired = wireless_pair(service, password,
                                       adb_path=args.adb_path,
                                       timeout=args.pair_timeout)
                result = wireless_connect(
                    adb_path=args.adb_path,
                    relay_port=getattr(args, "relay_port", None) or DEFAULT_RELAY_PORT,
                    timeout=getattr(args, "discover_timeout", 3.0),
                    adb_timeout=args.timeout)
            except AdbWirelessError as exc:
                emit({"ok": False, "error": exc.kind, "detail": exc.detail},
                     args.json)
                return 3
            emit({**result, "paired": paired["device"]}, args.json)
            return 0
        if args.adb_cmd == "connect":
            from .adb_wireless import (DEFAULT_RELAY_PORT, AdbWirelessError,
                                       connect as wireless_connect, stop_relay)
            relay_port = args.relay_port or DEFAULT_RELAY_PORT
            if args.stop_relay:
                emit({"ok": True, "stopped": stop_relay(relay_port)}, args.json)
                return 0
            try:
                result = wireless_connect(
                    adb_path=args.adb_path, relay_port=relay_port,
                    timeout=args.discover_timeout, adb_timeout=args.timeout)
            except AdbWirelessError as exc:
                emit({"ok": False, "error": exc.kind, "detail": exc.detail}, args.json)
                return 3
            emit(result, args.json)
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
    # The Tk window was retired: it was the only thing pulling the Tcl/Tk
    # runtime (~2.3 MB) into the frozen build. The Qt window replaces it.
    emit({"error": "ui-removed",
          "detail": "the Tk window is gone; use `flix gui` (Qt window)",
          "hint": "run RelayDesk with no arguments for the desktop window"},
         args.json)
    return 5


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
                                multicast_if=args.multicast_if,
                                model_port=args.model_port)
    except OSError as e:
        emit({"error": "env", "detail": f"{e}", "hint": "port in use? choose "
                                                        "--device-port/--api-port"}, False)
        return 5
    finally:
        store.close()


def cmd_models(args) -> int:
    """LAN model gateway: phones talk Anthropic Messages, cc-switch does the rest."""
    from .model_gateway import (lan_urls, load_config, models_list,
                                reset_token, run_gateway, save_config,
                                status_payload)
    action = getattr(args, "action", None) or "status"
    store = Store()
    if action == "doctor":
        try:
            if int(getattr(args, "watch", 0) or 0) > 0:
                return _models_watch(store, args)
            return _models_doctor(store, args)
        finally:
            store.close()
    if action == "list":
        try:
            rows = models_list(store)
        finally:
            store.close()
        if args.json:
            emit({"ok": True, "count": len(rows), "models": rows}, True)
            return 0
        if not rows:
            print("上游没有返回模型列表：检查 cc-switch 是否在运行（默认 127.0.0.1:15721）")
            return 0
        for row in rows:
            print(f"{row['short']:<32} {row['id']}")
            print(f"{'':<32} {row['display']}")
        print(f"\n共 {len(rows)} 个；手机 App 里填短名或完整 id 都可以。")
        return 0
    if action == "start":
        changes = {"enabled": True}
        if args.port:
            changes["port"] = args.port
        if args.upstream:
            changes["upstream"] = args.upstream
        if getattr(args, "allow_remote_upstream", False):
            changes["allow_remote_upstream"] = True
        if getattr(args, "upstream_token", None):
            changes["upstream_token"] = args.upstream_token
        if getattr(args, "max_concurrent", None) is not None:
            changes["max_concurrent"] = args.max_concurrent
        try:
            cfg = save_config(store, **changes)
        except ValueError as exc:
            emit({"error": "upstream", "detail": str(exc)}, args.json)
            store.close()
            return 2
        if args.json:
            print(json.dumps({"ok": True, "port": cfg["port"],
                              "upstream": cfg["upstream"],
                              "token": cfg["token"],
                              "urls": lan_urls(cfg["port"])},
                             ensure_ascii=False, indent=1), flush=True)
        # `models start` 是**前台常驻**（Ctrl+C 结束），不是卡住。用户第一次跑
        # 很容易以为它挂了：先明确说一句，并给出「想要后台托管该走哪条路」。
        if not args.json:
            print(f"前台运行中：网关监听 {cfg['port']}，Ctrl+C 结束。"
                  "\n  想让它在后台托管（关掉终端也继续跑），"
                  "用设置页「模型共享」开关，或 `flix serve`。", flush=True)
        return _models_serve(store, cfg)  # coroutine: runs until interrupted
    try:
        if action == "stop":
            from .model_gateway import save_config, stop_gateway
            cfg = save_config(store, enabled=False)
            result = stop_gateway(cfg["port"])
            payload = {"ok": result.get("ok", False), **result,
                       "port": cfg["port"]}
            if not args.json and result.get("daemon_hosted"):
                print("共享已关闭；网关由本机服务托管，重启本机服务后停止监听")
            emit(payload, args.json)
            return 0 if result.get("ok") else 5
        if action == "token":
            token = (reset_token(store) if args.reset
                     else load_config(store)["token"])
            emit({"ok": True, "token": token}, args.json)
            return 0
        payload = status_payload(store)
        if not args.json:
            enabled = bool(payload["enabled"])
            unauthorized = bool(payload.get("unauthorized"))
            if not enabled:
                state = "未开启（不共享给局域网）"
            elif unauthorized:
                state = "已开启，但端口上的网关不认当前令牌"
            elif payload["listening"]:
                state = "运行中"
            else:
                state = "已开启但未运行"
            print(f"状态: {state}")
            print(f"端口: {payload['port']}  上游: {payload['upstream']}")
            # 两个并发概念不同：max_concurrent 是本网关总并发，upstream_concurrency
            # 是压给上游（cc-switch）的闸门。只写「无限制」会让人以为没有任何上限。
            total = payload.get("max_concurrent") or 0
            gate = payload.get("upstream_concurrency") or 0
            print(f"并发: 本机 {('不限' if not total else total)}，"
                  f"上游闸门 {('不限' if not gate else gate)}"
                  f"（排队 {payload.get('queue_wait_s', '-')}s）")
            named = payload.get("urls_named") or []
            if named:
                print("地址: " + "、".join(
                    f"{row['url']}"
                    + (f"（{row['interface']}）" if row.get("interface") else "")
                    for row in named))
            else:
                print(f"地址: {', '.join(payload['urls'])}")
            print(f"令牌: {payload['token']}")
            print("协议: Anthropic Messages（/v1/messages）+ OpenAI 兼容"
                  "（/v1/chat/completions，手机端多数客户端用这个）")
            if unauthorized:
                print("上游链路: 无法确认（当前令牌通不过端口上那个网关的鉴权）")
            elif payload["listening"]:
                upstream_ok = payload.get("upstream_ok")
                upstream_text = ("正常" if upstream_ok
                                 else ("不可达" if upstream_ok is False
                                       else "未探测"))
                print(f"上游链路: {upstream_text}"
                      f"  模型: {payload.get('models', 0)} 个"
                      f"（flix models list 查看）")
                if payload.get("last_error"):
                    print(f"最近错误: {payload['last_error']}")
                prefixes = payload.get("relay_prefixes") or {}
                if prefixes:
                    print(f"中继: {len(prefixes)} 个前缀近期失败，"
                          f"这些路由暂不尝试（对端 VPN/服务未连？）："
                          f"{', '.join(prefixes)}")
            # 一行健康摘要：本机是否可用只看这一条就够（对端中继不算本机故障）
            if unauthorized:
                print("健康: 令牌对不上——要么被轮换过，要么这个端口上跑的是"
                      "另一个安装。用「复制手机配置」取当前令牌，或先停掉它再重试")
            elif not enabled:
                print("健康: 共享未开启（这是默认状态；要共享就开一下开关）")
            elif payload["listening"]:
                direct_dead = [
                    m for m, v in (payload.get("routes") or {}).items()
                    if not v.get("ok", True) and m.count("--") <= 1]
                visible = payload.get("models", 0)
                if direct_dead:
                    print(f"健康: 本机直连有 {len(direct_dead)} 条路由不可用"
                          f"（检查 cc-switch）")
                elif not visible:
                    print("健康: 没有可用模型（上游目录为空？）")
                else:
                    peer = payload.get("relay_prefixes") or {}
                    extra = (f"；{len(peer)} 个对端中继暂时不通（已从列表隐藏）"
                             if peer else "")
                    print(f"健康: 可用（{visible} 个模型）{extra}")
            stats = payload.get("stats") or {}
            if stats:
                mins = int(stats.get("uptime", 0)) // 60
                print(f"请求: {stats.get('requests', 0)}（成功 {stats.get('ok', 0)}，"
                      f"失败 {stats.get('failed', 0)}，拒绝 {stats.get('rejected', 0)}）"
                      f"  进行中 {stats.get('active', 0)}  已运行 {mins} 分钟")
                recent = stats.get("recent") or []
                if recent:
                    last = recent[-1]
                    print(f"最近拒绝: {last.get('kind', '?')} ← "
                          f"{last.get('remote') or '本机'}")
                # The last few proxied calls answer "手机为什么没反应":
                # status, latency and which model it actually asked for.
                calls = stats.get("recent_requests") or []
                for call in calls[-3:]:
                    model = call.get("to") or call.get("model") or "-"
                    print(f"  · {call.get('ms', 0)}ms  {call.get('status', 0)}  "
                          f"{model}  ← {call.get('remote') or '本机'}"
                          + ("  流式" if call.get("stream") else "")
                          + (f"  ({call['error']})" if call.get("error") else ""))
            if payload.get("detail") and not payload["listening"]:
                print(f"提示: {payload['detail']}")
            return 0
        emit(payload, args.json)
        return 0
    finally:
        store.close()


def _watch_baseline(samples: list[dict]) -> tuple[int, dict]:
    """找出计数器最后一次归零的位置，返回（重启次数，基线样本）。

    计数器是进程内的，网关重启就归零。直接拿末样本减首样本会算出负数
    （实测出现过「新增失败 -2」这种看不懂的数）。所以以「最后一次重启之后
    的第一个样本」当基线；没重启过就是第一个样本。
    """
    restart_at = [index for index, (prev, cur)
                  in enumerate(zip(samples, samples[1:]))
                  if cur["ok"] < prev["ok"] or cur["failed"] < prev["failed"]]
    if not restart_at:
        return 0, samples[0]
    return len(restart_at), samples[restart_at[-1] + 1]


def _models_watch(store, args) -> int:
    """持续采样本机网关，回答「跑久了会不会漂」。

    一次性 doctor 只能证明「此刻正常」；真实现场关心的是 30 分钟、2 小时后还正常吗。
    这里按间隔采样并在结束时给出漂移结论（模型数变化 / 新增失败 / active 泄漏 /
    直连路由变坏 / 真实补全变慢）。
    """
    import time

    from .model_gateway import load_config, probe

    cfg = load_config(store)
    duration = max(10, int(getattr(args, "watch", 0) or 0))
    interval = max(5, min(300, int(getattr(args, "interval", 30) or 30)))
    model = getattr(args, "model", None) or ""
    samples: list[dict] = []
    anomalies: list[str] = []
    started = time.monotonic()
    print(f"观察 {duration}s，每 {interval}s 采样一次（Ctrl-C 结束并给结论）", flush=True)

    def call_completion() -> tuple[bool, float, str]:
        import urllib.request
        # 200 而不是 64：推理模型可能把预算全花在思考上，64 会得到空正文，
        # 那是模型行为不是网关故障（实测误报过一次）。
        body = json.dumps({"model": model, "max_tokens": 200,
                           "messages": [{"role": "user",
                                         "content": "只回答两个字：通了"}]}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{cfg['port']}/v1/messages", data=body,
            headers={"content-type": "application/json",
                     "x-api-key": cfg["token"],
                     "anthropic-version": "2023-06-01"})
        began = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            blocks = data.get("content") or []
            text = "".join(b.get("text", "") for b in blocks
                           if b.get("type") == "text")
            thinking = "".join(b.get("thinking", "") for b in blocks
                               if b.get("type") == "thinking")
            elapsed = time.monotonic() - began
            if text.strip():
                return True, elapsed, ""
            # 推理模型在 max_tokens 小时会只回思考——链路是通的，不算故障。
            if thinking.strip():
                return True, elapsed, f"只回思考（{len(thinking)} 字，stop="
            return (False, elapsed,
                    f"HTTP 200 但正文与思考都为空（stop={data.get('stop_reason')}）")
        except Exception as exc:  # noqa: BLE001
            return False, time.monotonic() - began, f"{type(exc).__name__}"

    previous: dict | None = None
    index = 0
    try:
        while True:
            elapsed = time.monotonic() - started
            if elapsed > duration:
                break
            index += 1
            live = probe(cfg["port"], cfg["token"], timeout=8.0)
            routes = live.get("routes") or {}
            dead_direct = [m for m, v in routes.items()
                           if not v.get("ok", True) and m.count("--") <= 1]
            stats = live.get("stats") or {}
            sample = {
                "t": int(elapsed),
                "listening": bool(live.get("listening")),
                "models": live.get("models", 0),
                "catalog": live.get("catalog_models", 0),
                "ok": stats.get("ok", 0),
                "failed": stats.get("failed", 0),
                "rejected": stats.get("rejected", 0),
                "active": stats.get("active", 0),
                "dead_direct": len(dead_direct),
            }
            if index % 4 == 1:      # 每 4 次采一次真实补全，避免自己把自己压着
                good, seconds, error = call_completion()
                sample["chat_ms"] = int(seconds * 1000)
                sample["chat_ok"] = good
                if not good:
                    anomalies.append(f"t+{sample['t']}s 补全失败 {error}")
            samples.append(sample)

            if not sample["listening"]:
                anomalies.append(f"t+{sample['t']}s 网关不监听")
            if sample["dead_direct"]:
                anomalies.append(f"t+{sample['t']}s 直连路由不可用 "
                                 f"{sample['dead_direct']} 条")
            if previous is not None:
                if sample["failed"] > previous["failed"]:
                    anomalies.append(
                        f"t+{sample['t']}s 新增失败 "
                        f"{sample['failed'] - previous['failed']} 次")
                if sample["models"] != previous["models"]:
                    anomalies.append(
                        f"t+{sample['t']}s 模型数 {previous['models']}"
                        f"->{sample['models']}")
            previous = sample
            print(f"  t+{sample['t']:>4}s 模型={sample['models']:<3} "
                  f"目录={sample['catalog']:<3} ok={sample['ok']:<4} "
                  f"fail={sample['failed']:<3} rej={sample['rejected']:<3} "
                  f"active={sample['active']:<2} "
                  + (f"补全={sample['chat_ms']}ms" if "chat_ms" in sample else ""),
                  flush=True)
            remaining = duration - (time.monotonic() - started)
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))
    except KeyboardInterrupt:
        pass

    print()
    if not samples:
        print("没有采到样本")
        return 6
    models_were = [s["models"] for s in samples]
    chats = [s["chat_ms"] for s in samples if "chat_ms" in s]
    restarts, base = _watch_baseline(samples)
    if restarts:
        anomalies.append(
            f"观测期间网关重启过 {restarts} 次（计数归零，增量按重启后计）")
    summary = {
        "samples": len(samples),
        "seconds": samples[-1]["t"],
        "models_min": min(models_were),
        "models_max": max(models_were),
        "restarts": restarts,
        "failed_total": samples[-1]["failed"] - base["failed"],
        "rejected_total": samples[-1]["rejected"] - base["rejected"],
        "active_max": max(s["active"] for s in samples),
        "chat_p50_ms": sorted(chats)[len(chats) // 2] if chats else None,
        "chat_max_ms": max(chats) if chats else None,
        "dead_direct_max": max(s["dead_direct"] for s in samples),
        "anomalies": anomalies,
    }
    if getattr(args, "json", False):
        emit({"ok": not anomalies, "summary": summary, "samples": samples}, True)
    else:
        print(f"样本 {summary['samples']} 个，共 {summary['seconds']}s")
        if summary["restarts"]:
            print(f"注意: 观测期间网关重启过 {summary['restarts']} 次，"
                  "增量已按重启后重算")
        print(f"模型数 {summary['models_min']}–{summary['models_max']}，"
              f"active 峰值 {summary['active_max']}，"
              f"新增失败 {summary['failed_total']}，"
              f"新增拒绝 {summary['rejected_total']}")
        if summary["chat_p50_ms"] is not None:
            print(f"补全耗时 中位 {summary['chat_p50_ms']}ms / "
                  f"最大 {summary['chat_max_ms']}ms")
        if anomalies:
            print(f"异常 {len(anomalies)} 条：")
            for row in anomalies[:12]:
                print(f"  - {row}")
        else:
            print("结论: 无异常（模型数稳定、无新增失败、active 不泄漏）")
    return 0 if not anomalies else 6


def _models_doctor(store, args) -> int:
    """一条命令把整条链路体检一遍：配置 / 监听 / 上游 / 路由 / 真跑一次。

    现场排障时用户只知道「手机连不上」，而原因可能在六七个环节里。这里按顺序
    逐项给结论，并给出可执行的下一步建议。
    """
    import socket
    import time

    from .model_gateway import (lan_urls_named, load_config, probe,
                                route_health, relay_prefix_health)

    checks: list[dict] = []

    def add(name: str, ok: bool | None, detail: str, hint: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "hint": hint})

    cfg = load_config(store)
    port = cfg["port"]

    # 1. 配置
    add("配置", True,
        f"port={port} upstream={cfg['upstream']} "
        f"并发={cfg.get('upstream_concurrency')} 排队={cfg.get('queue_wait_s')}s "
        f"超时={cfg.get('upstream_timeout_s')}s "
        f"级联令牌={'已设置' if cfg.get('upstream_token') else '未设置'}",
        "关闭共享时先看 enabled")
    # 共享关着是用户的选择，不是故障：用提示级，不计入失败。
    add("共享开关", True if cfg.get("enabled") else None,
        "已开启" if cfg.get("enabled") else "未开启（不共享给局域网）",
        "flix models start 或设置页「模型共享」")
    # prefs 损坏会被备份成 *.bad-<时间戳> 并**轮换令牌**（配置本身救不回来）。
    # 后果是「手机突然全部 401」，而这条信息以前只在 stderr 出现一次、GUI 里看不到。
    # 这里把它变成一条可查的诊断，直接回答「为什么手机全掉了」。
    damaged = sorted(store.prefs_path.parent.glob(
        store.prefs_path.name + ".bad-*"), key=lambda q: q.stat().st_mtime)
    if damaged:
        newest = damaged[-1]
        when = time.strftime("%Y-%m-%d %H:%M",
                             time.localtime(newest.stat().st_mtime))
        add("配置完整性", None,
            f"{when} 的配置曾无法解析，已备份为 {newest.name}"
            "（当时令牌被重新生成，手机需重新填新令牌）",
            "如果手机突然全部 401，多半就是这个原因；旧文件留作取证")
    else:
        add("配置完整性", True, "配置文件可正常解析")

    # 2. 端口监听 + 谁在听
    listening = False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            listening = True
    except OSError:
        listening = False
    if not listening and not cfg.get("enabled"):
        # 共享关着、端口空着 —— 这是关掉共享后的正常状态，不该报成故障
        add("端口监听", None, f"127.0.0.1:{port} 未监听（共享已关闭，正常）",
            "要共享就开关一次「模型共享」或 flix models start")
    else:
        add("端口监听", listening, f"127.0.0.1:{port} " +
            ("可连" if listening else "连不上"),
            "flix models start 或设置页「模型共享」开关")

    # 3. 上游直连
    upstream = cfg["upstream"]
    up_ok = False
    up_detail = ""
    try:
        import urllib.error
        import urllib.request
        started = time.monotonic()
        req = urllib.request.Request(
            upstream.rstrip("/") + "/v1/models",
            headers={"anthropic-version": "2023-06-01",
                     "x-api-key": cfg.get("upstream_token") or "relaydesk"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        rows = payload.get("data") or payload.get("models") or []
        up_ok = bool(rows)
        up_detail = (f"{len(rows)} 个模型，{(time.monotonic() - started) * 1000:.0f}ms")
    except Exception as exc:  # noqa: BLE001
        up_detail = f"{type(exc).__name__}: {exc}"
    add("上游可读", up_ok, up_detail,
        "cc-switch 是否在跑（默认 15721）；级联时检查对端网关是否在线")

    # 4. 网关健康
    live = probe(port, cfg["token"], timeout=3.0)
    if live.get("unauthorized"):
        add("网关健康", False,
            "端口上的网关不认这个令牌（它只回了最小 /health）",
            "有两种可能：① 令牌被轮换过（「重置令牌」或 prefs 重建）；"
            "② 这个端口上跑的是**另一个安装**（不同用户/不同 APPDATA）。"
            "用桌面端「复制手机配置」拿到当前令牌，或先停掉那个网关再重试")
    elif not live.get("listening"):
        # 端口没在听时，「端口监听」那条已经报了根因；这里只补一句指引，
        # 不再重复记一条失败（否则同一件事报两次，看着像两个故障）。
        add("网关健康", None, "网关没在跑（见上面「端口监听」）",
            "flix models start 或设置页「模型共享」开关；开机自启的机器检查 "
            "RelayDeskServe 启动项")
    else:
        add("网关健康", bool(live.get("listening")),
            f"listening={live.get('listening')} models={live.get('models', 0)}",
            "端口在但不响应 = 进程卡死，杀掉后由看门狗拉起")
    stats = live.get("stats") or {}
    if stats:
        add("请求统计", True,
            f"ok={stats.get('ok')} failed={stats.get('failed')} "
            f"aborted={stats.get('aborted')} rejected={stats.get('rejected')} "
            f"active={stats.get('active')}")
    routes = live.get("routes") or route_health()
    dead = [k for k, v in routes.items() if not v.get("ok", True)]
    dead_direct = [m for m in dead if m.count("--") <= 1]
    # 只有「本机直连路由」坏掉才算本机故障；中继路由坏是**对端**的事，
    # 网关已经把它们从列表里摘掉了，不该让 flix models doctor 报红。
    add("路由健康", (None if dead and not dead_direct else not dead_direct),
        (f"{len(routes)} 条有记录，其中 {len(dead)} 条不通"
         + ("（均为对端中继）" if dead and not dead_direct else ""))
        if routes else "暂无记录",
        "直连路由不可用时检查 cc-switch（15721）与它上游的账号/额度")
    prefixes = live.get("relay_prefixes") or relay_prefix_health()
    if prefixes:
        add("中继前缀", None,
            f"{len(prefixes)} 个前缀暂时不通（对端问题，模型已从列表隐藏）："
            + ", ".join(prefixes),
            "对端机器/它的 VPN 是否正常；恢复后 10 分钟内自动重试")

    # 5. 真跑一次
    model = getattr(args, "model", None) or ""
    if not model:
        try:
            from .model_gateway import models_list
            rows = models_list(store)
            model = rows[0]["short"] if rows else ""
        except Exception:  # noqa: BLE001
            model = ""
    if model and live.get("listening"):
        try:
            import urllib.request
            body = json.dumps({"model": model, "max_tokens": 200,
                               "messages": [{"role": "user",
                                             "content": "只回答两个字：通了"}]}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/messages", data=body,
                headers={"content-type": "application/json",
                         "x-api-key": cfg["token"],
                         "anthropic-version": "2023-06-01"})
            started = time.monotonic()
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            text = "".join(b.get("text", "") for b in data.get("content", [])
                           if b.get("type") == "text")
            add("真实补全", bool(text.strip()),
                f"{model} -> {data.get('model')} "
                f"{(time.monotonic() - started):.1f}s {text.strip()[:20]!r}")
        except Exception as exc:  # noqa: BLE001
            hint = ("看 /health 的 last_error，以及 flix models status")
            if live.get("unauthorized"):
                hint = ("401 是因为端口上的网关不认当前令牌——"
                        "见上面「网关健康」那一条")
            add("真实补全", False, f"{model}: {type(exc).__name__}: {exc}", hint)
    else:
        add("真实补全", None, "跳过（网关未运行或没有可用模型）")

    # 5b. 同一件事再走一遍 OpenAI 协议：手机上的第三方客户端多数用这个，
    #     只验 Anthropic 等于没验「绝大多数人实际会走的路径」。
    if model and live.get("listening"):
        try:
            import urllib.request
            body = json.dumps({
                "model": model, "max_tokens": 200,
                "messages": [{"role": "user", "content": "只回答两个字：通了"}],
            }).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions", data=body,
                headers={"content-type": "application/json",
                         "authorization": f"Bearer {cfg['token']}"})
            started = time.monotonic()
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            text = (msg.get("content") or "").strip()
            reasoning = (msg.get("reasoning_content") or "").strip()
            elapsed = time.monotonic() - started
            if text:
                add("OpenAI 补全", True,
                    f"{model} -> {data.get('model')} {elapsed:.1f}s {text[:20]!r}")
            elif reasoning:
                add("OpenAI 补全", True,
                    f"只回思考（{len(reasoning)} 字，链路是通的） {elapsed:.1f}s")
            else:
                add("OpenAI 补全", False,
                    f"{model}: 200 但正文与思考都为空 "
                    f"(finish={choice.get('finish_reason')})",
                    "看 /health 的 last_error")
        except Exception as exc:  # noqa: BLE001
            add("OpenAI 补全", False, f"{model}: {type(exc).__name__}: {exc}",
                "手机用 OpenAI 协议时走的就是这条；先看 flix models status")
    else:
        add("OpenAI 补全", None, "跳过（网关未运行或没有可用模型）")

    # 6. 手机侧：给一段可以直接照抄进手机 App 的配置
    named = lan_urls_named(port)
    base = named[0]["url"] if named else f"http://127.0.0.1:{port}"
    add("手机地址", bool(named),
        "、".join(f"{r['url']}" + (f"（{r['interface']}）"
                                  if r.get("interface") else "") for r in named))
    hint_model = model or "glm-5.3-flash"
    phone_config = {
        "protocol_anthropic": "Anthropic Messages",
        "protocol_openai": "OpenAI 兼容",
        "base_url": base,
        "api_key": cfg["token"],
        "model": hint_model,
        "chat_path": "/v1/messages",
        "openai_path": "/v1/chat/completions",
        "self_test": f"{base}/?token={cfg['token']}",
    }
    add("手机配置", True, f"{base}（{hint_model}）")

    failed = [c for c in checks if c["ok"] is False]
    if getattr(args, "json", False):
        emit({"ok": not failed, "checks": checks,
              "phone": phone_config}, True)
    else:
        for check in checks:
            mark = {True: "OK  ", False: "FAIL", None: "--  "}[check["ok"]]
            print(f"[{mark}] {check['name']:<8} {check['detail']}")
            if check["hint"] and check["ok"] is not True:
                print(f"         建议: {check['hint']}")
        print()
        if live.get("listening") and cfg.get("enabled"):
            print("手机端照抄这段（协议选 Anthropic；只支持 OpenAI 的 App 选 OpenAI 兼容）：")
            print(f"  Base URL : {phone_config['base_url']}")
            print(f"  API Key  : {phone_config['api_key']}")
            print(f"  模型      : {phone_config['model']}")
            print(f"  自检页    : {phone_config['self_test']}")
        else:
            # 别把用不了的令牌递给用户照抄：网关没跑或共享关着时，这些配置用不上。
            why = "共享已关闭" if not cfg.get("enabled") else "网关没在跑"
            print(f"手机端配置暂不可用（{why}）：先开启「模型共享」，再回来抄这段。")
        print()
        if failed:
            print(f"结论: {len(failed)} 项异常 -> "
                  f"{', '.join(c['name'] for c in failed)}")
        else:
            print("结论: 全部正常")
    return 0 if not failed else 6


async def _models_serve(store, cfg) -> int:
    from .model_gateway import run_gateway
    try:
        await run_gateway(store, cfg["port"], cfg["upstream"])
    except OSError as exc:
        emit({"error": "port", "detail": f"{exc}",
              "hint": f"端口 {cfg['port']} 被占用？用 --port 换一个"}, False)
        return 5
    finally:
        store.close()
    return 0


def _normalize_shell_argv(argv: list[str]) -> list[str]:
    """Let `flix shell <to> --timeout 5 -- cmd` parse like argparse expects.

    `shell` uses REMAINDER for the command, which would otherwise swallow any
    option placed between the target and the `--` separator. Move those
    options in front of the target so both orders work.
    """
    try:
        i = argv.index("shell")
    except ValueError:
        return argv
    head, rest = argv[:i + 1], argv[i + 1:]
    if not rest or rest[0].startswith("--"):
        return argv
    tail = rest[1:]
    if "--" not in tail:
        return argv
    j = tail.index("--")
    flags, command = tail[:j], tail[j:]
    return [*head, *flags, rest[0], *command]


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

    p = add("shell", "run one command on a paired device (exec only, no PTY)",
            cmd_shell)
    p.add_argument("to", help="fingerprint (prefix ok) or ip[:port]")
    p.add_argument("--timeout", type=float, default=30.0,
                   help="seconds before the process tree is killed (default 30)")
    p.add_argument("--pty", action="store_true",
                   help="interactive terminal (ConPTY) instead of one-shot exec")
    p.add_argument("--shell", default="cmd", metavar="NAME",
                   help="shell for --pty: cmd|powershell|pwsh (default cmd)")
    p.add_argument("--port", type=int, default=DEVICE_PORT)
    p.add_argument("command", nargs=argparse.REMAINDER,
                   help="command to run; put '--' before it")

    p = add("upgrade", "replace a paired device's RelayDesk.exe "
                       "(SHA256 check, health check, automatic rollback)",
            cmd_upgrade)
    p.add_argument("to")
    p.add_argument("--file", required=True, help="local RelayDesk.exe to ship")
    p.add_argument("--wait", type=int, default=60,
                   help="seconds to wait for the device port (default 60)")
    p.add_argument("--fresh-env", action="store_true",
                   help="clear PyInstaller's extraction-dir variables before "
                        "restart (needed for new native deps, but on some hosts "
                        "the fresh unpack never comes up)")
    p.add_argument("--port", type=int, default=DEVICE_PORT)

    p = add("update", "update this install from the GitHub release page "
                      "(sha256 + health check + rollback)", cmd_update)
    p.add_argument("--check", action="store_true",
                   help="report whether a newer release exists, install nothing "
                        "(exit 10 when one does)")
    p.add_argument("--force", action="store_true",
                   help="reinstall even when the running build matches the release")
    p.add_argument("--dry-run", action="store_true",
                   help="download and verify, then stop before touching the exe")
    p.add_argument("--exe", default=None, metavar="PATH",
                   help="RelayDesk.exe to update (default: detect running/known copy)")
    p.add_argument("--asset", default=None, metavar="REGEX",
                   help="pick the release asset by name (default: this platform's zip)")
    p.add_argument("--repo", default="XinwangTao/flix-cli",
                   help="owner/name to poll (default: XinwangTao/flix-cli)")
    p.add_argument("--api-base", default="https://api.github.com")
    p.add_argument("--token", default=None,
                   help="GitHub token (or GITHUB_TOKEN) to lift the 60 req/h limit")
    p.add_argument("--mirror", action="append", default=None, metavar="PREFIX",
                   help="download mirror prefix tried after a direct failure "
                        "(repeatable; default gh-proxy.com, ghfast.top)")
    p.add_argument("--pre", action="store_true", help="allow pre-releases")
    p.add_argument("--no-restart", action="store_true",
                   help="update the files but do not relaunch what was stopped")
    p.add_argument("--start", action="store_true",
                   help="launch the app after updating even if it was not running")
    p.add_argument("--wait", type=float, default=60.0,
                   help="seconds to wait for a restarted build to come up (60)")
    p.add_argument("--installed-version", default=None,
                   help="treat this as the installed version (when no state file "
                        "exists and the windowed build cannot print --version)")
    p.add_argument("--state-dir", default=None, metavar="DIR",
                   help="where update_state.json / update.log live "
                        "(default: %%APPDATA%%\\flixcli)")
    p.add_argument("--downloads-dir", default=None, metavar="DIR",
                   help="where the release zip is cached (default: %%TEMP%%\\relaydesk-update)")
    p.add_argument("--quiet", action="store_true",
                   help="log to file only (for scheduled tasks)")

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
    p.add_argument("--link", action="store_true",
                   help="also print the relaydesk://pair URI the Android app scans")
    p.add_argument("--with-model", action="store_true",
                   help="put the LAN model-gateway address and token in that URI, "
                        "so one scan also configures model sharing")

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

    p = add("outbox", "inspect the durable delivery queue "
                      "(text bubbles that could not be delivered yet)",
            cmd_outbox)
    p.add_argument("--retry", metavar="ID", help="put a failed/expired row back in the queue")
    p.add_argument("--drop", metavar="ID", help="drop a row without sending it")
    p.add_argument("--limit", type=int, default=50)
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
    p_conn = adb_sub.add_parser(
        "connect", help="wireless debugging: discover the phone and connect "
                        "through the loopback relay")
    p_conn.add_argument("--relay-port", type=int, default=None,
                        help="loopback relay port (default 40009)")
    p_conn.add_argument("--discover-timeout", type=float, default=3.0,
                        help="seconds to wait for each mDNS lookup")
    p_conn.add_argument("--stop-relay", action="store_true",
                        help="stop a relay started by an earlier connect")
    p_qrpair = adb_sub.add_parser(
        "pair-qr", help="show the official ADB pairing QR, then pair + connect")
    p_qrpair.add_argument("--pair-timeout", type=float, default=90.0,
                          help="seconds to wait for the phone to scan")
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
    p.add_argument("--model-port", type=int, default=0,
                   help="also serve the LAN model gateway on this port "
                        "(e.g. 18993)")
    p.add_argument("--multicast-if", default=None, metavar="IP",
                   help="interface IP for discovery multicast (default: auto)")
    p.set_defaults(fn=cmd_serve)

    p = add("models", "LAN model gateway for phones: Anthropic Messages "
                      "proxied to the local cc-switch proxy", cmd_models)
    # help= 只出现在 `flix --help` 的命令列表里；子命令自己的 --help 看不到。
    # 用户最容易踩的是「models start 占住终端以为卡死」，所以写进 epilog。
    p.epilog = ("`models start` runs in the FOREGROUND until Ctrl+C. "
                "For background hosting (survives closing the terminal) use the "
                "settings toggle or `flix serve`.")
    p.add_argument("action", nargs="?", default="status",
                   choices=["start", "stop", "status", "token", "list",
                            "doctor"])
    p.add_argument("--port", type=int, default=None,
                   help="listen port (default 18993)")
    p.add_argument("--upstream", default=None,
                   help="local proxy to forward to (default http://127.0.0.1:15721)")
    p.add_argument("--reset", action="store_true",
                   help="token: mint a new token")
    p.add_argument("--allow-remote-upstream", action="store_true",
                   help="allow an upstream outside 127.0.0.1 (default: loopback "
                        "only); needed for LAN cascading")
    p.add_argument("--watch", type=int, default=0, metavar="SECONDS",
                   help="doctor: 持续观察 N 秒（默认 0 = 只体检一次）；"
                        "用来量化长时间运行是否漂移")
    p.add_argument("--interval", type=int, default=30, metavar="SECONDS",
                   help="doctor --watch 的采样间隔（默认 30）")
    p.add_argument("--upstream-token", default=None, metavar="TOKEN",
                   help="token to present to the upstream when it is itself a "
                        "RelayDesk gateway (LAN cascade); your own client token "
                        "is never forwarded")
    p.add_argument("--max-concurrent", type=int, default=None,
                   help="in-flight cap before answering 429; 0 = no cap (default)")

    args = ap.parse_args(_normalize_shell_argv(
        list(sys.argv[1:] if argv is None else argv)))
    try:
        result = args.fn(args)
        if asyncio.iscoroutine(result):
            result = asyncio.run(result)
        return result
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
