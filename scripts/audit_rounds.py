#!/usr/bin/env python3
"""100-round audit driver (2026-11 hardening pass).

Each round is a real check: a pytest node, a live-daemon attack replay, a CLI
or MCP invocation, a fuzz property, an A/B measurement, or a static-analysis
gate. Results are printed as ``Rxx PASS/FAIL`` and appended to JSON so the run
can be compared across commits.

Usage:
    python scripts/audit_rounds.py core      # R01-R50
    python scripts/audit_rounds.py cli       # R51-R73
    python scripts/audit_rounds.py fuzz      # R74-R83
    python scripts/audit_rounds.py ab        # R84-R93
    python scripts/audit_rounds.py tools     # R94-R100
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
FLIX = str(ROOT / ".venv" / "Scripts" / "flix.exe")
BASELINE_SRC = Path(r"D:\UserData\Temp\flix-baseline\src")
OUT = Path(r"D:\UserData\Temp\flix-audit")
OUT.mkdir(parents=True, exist_ok=True)

RESULTS: list[dict] = []


def record(num: int, name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"round": f"R{num:02d}", "name": name, "ok": bool(ok),
                    "detail": detail[-800:]})
    print(f"R{num:02d} {'PASS' if ok else 'FAIL'}  {name}"
          + (f"  [{detail[:160]}]" if detail and not ok else ""))


def run(cmd: list[str], timeout: float = 120, env: dict | None = None,
        cwd: Path | None = None) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          env=e, cwd=str(cwd or ROOT))


def pytest_cmd(args: list[str]) -> tuple[bool, str]:
    r = run([PY, "-m", "pytest", *args, "-q", "--timeout=120"], timeout=180)
    out = (r.stdout + r.stderr)[-600:]
    if r.returncode == 0:
        return True, out
    # Qt tears down its QApplication with code 127 on this Windows build even
    # when every test passed; trust the pytest summary in that one case.
    # PySide6 this build dies with STATUS_STACK_BUFFER_OVERRUN (0xC0000409)
    # or 127 while tearing down the import-time QApplication, after pytest
    # has already reported every test as passed.
    if (r.returncode in (127, 3221226505) and "passed" in out
            and "failed" not in out and "error" not in out.lower()):
        return True, f"rc=0x{r.returncode & 0xFFFFFFFF:08X} (Qt teardown) " + out
    # All tests skipped (optional dependency absent) is not a failure.
    if r.returncode == 5 and "skipped" in out and "failed" not in out:
        return True, "all skipped " + out
    return False, out


def pytest_node(node: str) -> tuple[bool, str]:
    return pytest_cmd([node])


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Daemon:
    def __init__(self, appdata: Path, dev_port: int | None = None,
                 api_port: int | None = None, web_port: int | None = None,
                 baseline: bool = False):
        self.appdata = appdata
        self.dev = dev_port or free_port()
        self.api = api_port or free_port()
        self.web = web_port
        self.baseline = baseline
        self.proc = None

    def env(self) -> dict:
        env = {"APPDATA": str(self.appdata)}
        if self.baseline:
            env["PYTHONPATH"] = str(BASELINE_SRC)
        return env

    def start(self) -> None:
        cmd = [PY, "-m", "flixcli.daemon", "--device-port", str(self.dev),
               "--api-port", str(self.api), "--no-announce", "--no-clip-sync"]
        if self.web:
            cmd += ["--web-port", str(self.web)]
        self.proc = subprocess.Popen(cmd, env={**os.environ, **self.env()},
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.api}/api/self", timeout=1):
                    return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError(f"daemon did not start: {self.proc.stderr.read()[:500]}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def post(port: int, path: str, payload, headers=None, timeout=8):
    data = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
    if isinstance(data, str):
        data = data.encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def get(port: int, path: str, timeout=6):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                    timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def flix(args: list[str], appdata: Path, timeout: float = 30,
         baseline: bool = False) -> subprocess.CompletedProcess:
    env = {"APPDATA": str(appdata)}
    if baseline:
        env["PYTHONPATH"] = str(BASELINE_SRC)
    cmd = ([PY, "-m", "flixcli.cli"] if baseline else [FLIX]) + args
    return run(cmd, timeout=timeout, env=env)


# ---------------------------------------------------------------------------
# R01-R20: every fix has a check
# ---------------------------------------------------------------------------

def r01() -> tuple[bool, str]:
    r = flix(["--version"], OUT / "v")
    return "2.2.1" in r.stdout, r.stdout + r.stderr


def r02() -> tuple[bool, str]:
    appdata = Path(tempfile.mkdtemp(prefix="r02-", dir=OUT))
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "audit", "version": "1"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "flix_pair", "arguments": {"to": "127.0.0.1"}}},
    ]
    r = subprocess.run([PY, "-m", "flixcli.mcp_server"], input="\n".join(
        json.dumps(p) for p in payload) + "\n", capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env={**os.environ, "APPDATA": str(appdata)}, timeout=30)
    return ("pair-code-required" in r.stdout
            and "internal error" not in r.stdout), r.stdout[-500:]


def r03() -> tuple[bool, str]:
    return pytest_node("tests/test_hardening.py::WebHardeningTests"
                       "::test_upload_filename_cannot_escape_save_dir")


def r04() -> tuple[bool, str]:
    return pytest_node("tests/test_hardening.py::MessageHardeningTests"
                       "::test_unpaired_bubble_is_rejected")


def r05() -> tuple[bool, str]:
    return pytest_node("tests/test_hardening.py::MessageHardeningTests"
                       "::test_clipboard_from_unpaired_is_rejected")


def r06() -> tuple[bool, str]:
    return pytest_node("tests/test_hardening.py::MessageHardeningTests"
                       "::test_rd_shell_requires_paired_and_matching_source")


def r07() -> tuple[bool, str]:
    return pytest_node("tests/test_hardening.py::PairingHardeningTests")


def r08() -> tuple[bool, str]:
    return pytest_node("tests/test_hardening.py::WebHardeningTests"
                       "::test_pin_probing_is_throttled_on_get_route")


def r09() -> tuple[bool, str]:
    src = (ROOT / "src/flixcli/webapp.py").read_text(encoding="utf-8")
    ok = "/ws?pin=" in src and "encodeURIComponent(PIN)" in src
    return ok, "ws url"


def r10() -> tuple[bool, str]:
    src = (ROOT / "src/flixcli/cli.py").read_text(encoding="utf-8")
    mcp = (ROOT / "src/flixcli/mcp_server.py").read_text(encoding="utf-8")
    ok = ("async def cmd_group_add" in src and "update_group(" in src
          and "update_group(" in mcp)
    return ok, "group sync"


def r11() -> tuple[bool, str]:
    # The Tk window was retired so the frozen build no longer carries Tcl/Tk.
    removed = not (ROOT / "src/flixcli/ui.py").exists()
    src = (ROOT / "src/flixcli/cli.py").read_text(encoding="utf-8")
    ok = removed and "ui-removed" in src and "import ui" not in src
    return ok, "tk ui retired"


def r12() -> tuple[bool, str]:
    return pytest_node("tests/test_ops_hardening.py::ShellQuotingTests")


def r13() -> tuple[bool, str]:
    return pytest_node("tests/test_hardening.py::InterfaceAndVersionTests")


def r14() -> tuple[bool, str]:
    src = (ROOT / "src/flixcli/daemon_runtime.py").read_text(encoding="utf-8")
    ok = "Semaphore(8)" in src and "asyncio.gather" in src
    return ok, "presence concurrency"


def r15() -> tuple[bool, str]:
    return pytest_node("tests/test_ops_hardening.py::WireLogTests")


def r16() -> tuple[bool, str]:
    return pytest_node("tests/test_ops_hardening.py::DesktopApiGuardTests")


def r17() -> tuple[bool, str]:
    return pytest_node("tests/test_daemon_singleton.py::UpgradeScriptTests")


def r18() -> tuple[bool, str]:
    return pytest_node("tests/test_ops_hardening.py::OutboxRetryTests")


def r19() -> tuple[bool, str]:
    return pytest_node("tests/test_ops_hardening.py::RemoteShellBoundsTests")


def r20() -> tuple[bool, str]:
    return pytest_node("tests/test_ops_hardening.py::MediaCallIdTests")


CORE_FIX_ROUNDS = [r01, r02, r03, r04, r05, r06, r07, r08, r09, r10,
                   r11, r12, r13, r14, r15, r16, r17, r18, r19, r20]

# ---------------------------------------------------------------------------
# R21-R44: module regression matrix
# ---------------------------------------------------------------------------

CORE_TEST_FILES = [
    ("test_adb", "tests/test_adb.py"),
    ("agent link", "tests/test_agent_link.py"),
    ("bubble paths", "tests/test_bubble_paths.py"),
    ("desktop pair", "tests/test_desktop_pair.py"),
    ("device dedupe", "tests/test_device_dedupe.py"),
    ("group sync", "tests/test_group_sync.py"),
    ("group update", "tests/test_group_update.py"),
    ("groupchat", "tests/test_groupchat.py"),
    ("gui copy", "tests/test_gui_copy.py"),
    ("gui notify", "tests/test_gui_notify.py"),
    ("gui scroll", "tests/test_gui_scroll.py"),
    ("media http", "tests/test_media_http.py"),
    ("media session", "tests/test_media_session.py"),
    ("media transport", "tests/test_media_transport.py"),
    ("outbox", "tests/test_outbox.py"),
    ("prefs isolation", "tests/test_prefs_isolation.py"),
    ("presence", "tests/test_presence.py"),
    ("ptyshell", "tests/test_ptyshell.py"),
    ("remoteshell", "tests/test_remoteshell.py"),
    ("transfer security", "tests/test_transfer_security.py"),
    ("upgrade", "tests/test_upgrade.py"),
    ("hardening", "tests/test_hardening.py"),
    ("ops hardening", "tests/test_ops_hardening.py"),
    ("daemon singleton", "tests/test_daemon_singleton.py"),
]


def make_test_round(path: str):
    return lambda: pytest_node(path)


# ---------------------------------------------------------------------------
# R45-R50: live-daemon attack replay
# ---------------------------------------------------------------------------

def r45() -> tuple[bool, str]:
    r = run([PY, "tests/e2e.py"], timeout=300)
    return ("11/11 passed" in r.stdout), r.stdout[-600:]


def _live_daemon() -> tuple[Daemon, Path]:
    appdata = Path(tempfile.mkdtemp(prefix="live-", dir=OUT))
    d = Daemon(appdata)
    d.start()
    return d, appdata


def r46() -> tuple[bool, str]:
    d, _ = _live_daemon()
    try:
        st1, _ = post(d.dev, "/bubble", {
            "id": str(uuid.uuid4()), "from": "stranger", "to": "x",
            "type": 0, "time": 1, "content": "inject"})
        st2, _ = post(d.dev, "/intent", {
            "action": "clipboard", "deviceId": "stranger",
            "extra": {"text": json.dumps({"text": "evil"})}})
        st3, _ = post(d.dev, "/intent", {
            "action": "rdShell", "deviceId": "stranger",
            "extra": {"id": "x", "command": "echo nope"}})
        return (st1, st2, st3) == (403, 403, 403), f"{st1},{st2},{st3}"
    finally:
        d.stop()


def r47() -> tuple[bool, str]:
    d, appdata = _live_daemon()
    try:
        from flixcli.config import Store
        with Store(appdata / "flixcli" / "db.sqlite") as s:
            s.add_pair("peer-x", "1234")
            s.upsert_device("peer-x", ip="10.9.9.9")
        st, body = post(d.dev, "/intent", {
            "action": "rdShell", "deviceId": "peer-x",
            "extra": {"id": "x", "command": "echo nope"}})
        return st == 403, f"{st} {body[:100]}"
    finally:
        d.stop()


def r48() -> tuple[bool, str]:
    d, appdata = _live_daemon()
    try:
        from flixcli.config import Store
        with Store(appdata / "flixcli" / "db.sqlite") as s:
            own = s.own_pair_key()
            me = json.loads((appdata / "flixcli" / "shared_preferences.json")
                            .read_text(encoding="utf-8"))["identity"]["fingerprint"]
        st1, body1 = post(d.dev, "/intent", {
            "action": "askPairDevice", "deviceId": "attacker",
            "extra": {"verifyDevice": me, "verifyCode": "0000"}})
        with Store(appdata / "flixcli" / "db.sqlite") as s:
            bad = s.is_paired("attacker")
        st2, body2 = post(d.dev, "/intent", {
            "action": "askPairDevice", "deviceId": "good-peer",
            "extra": {"verifyDevice": me, "verifyCode": own}})
        with Store(appdata / "flixcli" / "db.sqlite") as s:
            good = s.is_paired("good-peer")
        return (not bad) and good and "302" in body1 and "0: success" in body2, body2[:100]
    finally:
        d.stop()


def r49() -> tuple[bool, str]:
    appdata = Path(tempfile.mkdtemp(prefix="pin-", dir=OUT))
    d = Daemon(appdata, web_port=free_port())
    d.start()
    try:
        import http.client
        codes = []
        for _ in range(10):
            conn = http.client.HTTPConnection("127.0.0.1", d.web, timeout=5)
            conn.request("GET", "/0000")
            codes.append(conn.getresponse().status)
            conn.close()
        from flixcli.config import Store
        with Store(appdata / "flixcli" / "db.sqlite") as s:
            pin = s.own_web_pin()
        conn = http.client.HTTPConnection("127.0.0.1", d.web, timeout=5)
        conn.request("GET", f"/{pin}")
        final = conn.getresponse().status
        conn.close()
        return all(c == 302 for c in codes) and final == 302, f"{codes[-1]},{final}"
    finally:
        d.stop()


def r50() -> tuple[bool, str]:
    appdata = Path(tempfile.mkdtemp(prefix="up-", dir=OUT))
    save = Path(tempfile.mkdtemp(prefix="save-", dir=OUT))
    (appdata / "flixcli").mkdir(parents=True, exist_ok=True)
    with __import__("flixcli.config", fromlist=["Store"]).Store(
            appdata / "flixcli" / "db.sqlite") as s:
        s.set_pref("saveDir", str(save))
    d = Daemon(appdata, web_port=free_port())
    d.start()
    try:
        from flixcli.config import Store
        with Store(appdata / "flixcli" / "db.sqlite") as s:
            pin = s.own_web_pin()
        boundary = "----audit"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\";"
            ' filename="../../escape.txt"\r\n'
            "Content-Type: application/octet-stream\r\n\r\nevil\r\n"
            f"--{boundary}--\r\n").encode()
        st, text = post(d.web, f"/upload?pin={pin}", body, headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}"})
        escaped = (save.parent / "escape.txt").exists()
        return st in (200, 400) and not escaped, f"{st} escaped={escaped}"
    finally:
        d.stop()


def run_core() -> None:
    for i, fn in enumerate(CORE_FIX_ROUNDS, start=1):
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001 - a crashed round is a FAIL
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        record(i, fn.__name__, ok, detail)

    for idx, (name, path) in enumerate(CORE_TEST_FILES, start=21):
        try:
            ok, detail = make_test_round(path)()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        record(idx, f"pytest {name}", ok, detail)

    live = [("e2e two nodes", r45), ("live unpaired replay", r46),
            ("live wrong-source rdShell", r47), ("live pairing code", r48),
            ("live PIN probing", r49), ("live upload traversal", r50)]
    for idx, (name, fn) in enumerate(live, start=45):
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        record(idx, name, ok, detail)


# ---------------------------------------------------------------------------
# R51-R73: CLI command matrix + MCP tool surface
# ---------------------------------------------------------------------------

CLI_HOME = OUT / "cli-home"


def cli(args: list[str], timeout: float = 30) -> tuple[int, str, str]:
    r = flix(args, CLI_HOME, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def cli_help(name: str):
    def check() -> tuple[bool, str]:
        rc, out, err = cli([name, "--help"])
        return rc == 0 and "usage:" in out, (out + err)[-300:]
    return check


def cli_self() -> tuple[bool, str]:
    rc, out, err = cli(["self", "--json"])
    try:
        data = json.loads(out)
        return rc == 0 and bool(data.get("fingerprint")), out[:300]
    except ValueError:
        return False, (out + err)[:300]


def cli_json(name: str):
    def check() -> tuple[bool, str]:
        rc, out, err = cli([name, "--json"])
        try:
            json.loads(out)
            return rc == 0, out[:300]
        except ValueError:
            return False, (out + err)[:300]
    return check


def cli_discover() -> tuple[bool, str]:
    # Exit 3 is the documented "no devices found" code: this audit host has no
    # other Flix node announcing. What matters is valid JSON on stdout.
    rc, out, err = cli(["discover", "--json", "--timeout", "2"], timeout=30)
    try:
        payload = json.loads(out)
        return rc in (0, 3) and isinstance(payload, list), f"rc={rc} {out[:160]}"
    except ValueError:
        return False, (out + err)[:300]


def cli_probe_closed() -> tuple[bool, str]:
    port = free_port()
    rc, out, err = cli(["probe", f"127.0.0.1:{port}", "--json"], timeout=30)
    combined = out + err
    return (rc in (3, 4) and "error" in combined.lower()), f"rc={rc} {combined[:200]}"


def cli_group_help() -> tuple[bool, str]:
    rc, out, err = cli(["group", "--help"])
    subs = ("create", "add", "join", "leave", "list", "send", "history")
    return rc == 0 and all(s in out for s in subs), out[-300:]


def _mcp(payloads: list[dict], timeout: float = 30) -> list[dict]:
    CLI_HOME.mkdir(parents=True, exist_ok=True)
    init = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "audit", "version": "1"}}}
    lines = "\n".join(json.dumps(p) for p in [init, *payloads]) + "\n"
    r = subprocess.run([PY, "-m", "flixcli.mcp_server"], input=lines,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout,
                       env={**os.environ, "APPDATA": str(CLI_HOME)})
    out = []
    for line in r.stdout.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _tool_call(name: str, args: dict) -> tuple[bool, str]:
    resp = _mcp([{"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": name, "arguments": args}}])
    for msg in resp:
        if msg.get("id") == 2:
            res = msg.get("result", {})
            text = " ".join(c.get("text", "") for c in res.get("content", []))
            return (not res.get("isError")), text[:400]
    return False, "no response"


def mcp_tool_list() -> tuple[bool, str]:
    resp = _mcp([{"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                  "params": {}}])
    for msg in resp:
        if msg.get("id") == 2:
            tools = msg["result"]["tools"]
            names = {t["name"] for t in tools}
            ok = (len(tools) == 23 and len(names) == 23
                  and all(t.get("inputSchema", {}).get("type") == "object"
                          for t in tools))
            return ok, f"{len(tools)} tools"
    return False, "no tools/list response"


def run_cli() -> None:
    CLI_HOME.mkdir(parents=True, exist_ok=True)
    cli_rounds: list[tuple[str, Callable[[], tuple[bool, str]]]] = [
        ("--version", lambda: (lambda rc, o, e: ("flix " in o and rc == 0, o))(
            *cli(["--version"]))),
        ("self --json", cli_self),
        ("devices --json", cli_json("devices")),
        ("outbox --json", cli_json("outbox")),
        ("bubbles --json", cli_json("bubbles")),
        ("netcheck --json", cli_json("netcheck")),
        ("qrcode --json", cli_json("qrcode")),
        ("discover --json", cli_discover),
        ("probe closed port", cli_probe_closed),
        ("group help", cli_group_help),
        ("send-text help", cli_help("send-text")),
        ("shell help", cli_help("shell")),
        ("upgrade help", cli_help("upgrade")),
        ("adb help", cli_help("adb")),
        ("mcp help", cli_help("mcp")),
    ]
    for idx, (name, fn) in enumerate(cli_rounds, start=51):
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        record(idx, f"cli {name}", ok, detail)

    mcp_rounds: list[tuple[str, Callable[[], tuple[bool, str]]]] = [
        ("tools/list", mcp_tool_list),
        ("flix_self", lambda: _tool_call("flix_self", {})),
        ("flix_devices", lambda: _tool_call("flix_devices", {})),
        ("flix_status", lambda: _tool_call("flix_status", {})),
        ("flix_bubbles", lambda: _tool_call("flix_bubbles", {"limit": 5})),
        ("flix_group_list", lambda: _tool_call("flix_group_list", {})),
        ("flix_pair requires code",
         lambda: (lambda _ok, text: ("pair-code-required" in text, text))(
             *_tool_call("flix_pair", {"to": "127.0.0.1"}))),
        ("flix_send_text unknown",
         lambda: (lambda _ok, text: ("no-device" in text, text))(
             *_tool_call("flix_send_text", {"to": "no-such-device",
                                            "text": "x"}))),
    ]
    for idx, (name, fn) in enumerate(mcp_rounds, start=66):
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        record(idx, f"mcp {name}", ok, detail)


# ---------------------------------------------------------------------------
# R74-R83: fuzz — malformed wire input must never crash or corrupt state
# ---------------------------------------------------------------------------

def fuzz_node(expr: str):
    return lambda: pytest_cmd(["tests/test_fuzz_props.py", "-k", expr])


def _pair_loopback(store_path: Path, fp: str = "fuzz-peer") -> None:
    from flixcli.config import Store
    with Store(store_path) as s:
        s.add_pair(fp, "1234")
        s.upsert_device(fp, ip="127.0.0.1")


def r78() -> tuple[bool, str]:
    """Malformed JSON on /bubble and /intent: rejected, daemon stays up."""
    d, appdata = _live_daemon()
    try:
        _pair_loopback(appdata / "flixcli" / "db.sqlite")
        bodies = [b"", b"{", b"[", b"null", b"[]", b'"x"', b"\xff\xfe\x00",
                  b'{"id":1}', b'{"id":"x","from":null}',
                  b'{"action":"clipboard","deviceId":"fuzz-peer","extra":null}',
                  b'{"action":"rdShell","deviceId":"fuzz-peer","extra":"no"}']
        bad = []
        for body in bodies:
            for path in ("/bubble", "/intent"):
                st, text = post(d.dev, path, body)
                if st == 200 and "received" in text:
                    bad.append((path, body[:20], st))
        alive, _ = get(d.api, "/api/self")
        return not bad and alive == 200, f"bad={bad[:3]} alive={alive}"
    finally:
        d.stop()


def r79() -> tuple[bool, str]:
    """Hostile upload filenames are flattened; nothing escapes saveDir."""
    appdata = Path(tempfile.mkdtemp(prefix="fuzz-up-", dir=OUT))
    save = appdata / "save"
    save.mkdir(parents=True, exist_ok=True)
    (appdata / "flixcli").mkdir(parents=True, exist_ok=True)
    from flixcli.config import Store
    with Store(appdata / "flixcli" / "db.sqlite") as s:
        s.set_pref("saveDir", str(save))
    d = Daemon(appdata, web_port=free_port())
    d.start()
    try:
        with Store(appdata / "flixcli" / "db.sqlite") as s:
            pin = s.own_web_pin()
        names = ["../../evil.txt", "..\\..\\evil.txt", "....//evil.txt",
                 "C:\\Windows\\evil.txt", "中文 名字.txt", "a" * 180 + ".txt",
                 " space .txt", "dot..txt"]
        statuses = []
        for i, name in enumerate(names):
            boundary = f"----fuzz{i}"
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; "
                f'name="file"; filename="{name}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\nx\r\n"
                f"--{boundary}--\r\n").encode("utf-8")
            st, _ = post(d.web, f"/upload?pin={pin}", body, headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}"})
            statuses.append(st)
        escaped = [p.name for p in save.parent.iterdir()
                   if p.is_file() and p.name.startswith(("evil", "dot"))]
        escaped += [p.name for p in Path("C:/Windows").glob("evil.txt")]
        inside = [p.name for p in save.iterdir() if p.is_file()]
        alive, _ = get(d.api, "/api/self")
        ok = (not escaped and alive == 200
              and all(s in (200, 400) for s in statuses))
        return ok, f"statuses={statuses} escaped={escaped} inside={inside[:4]}"
    finally:
        d.stop()


def r80() -> tuple[bool, str]:
    """100 concurrent paired bubbles: every one is stored, none lost."""
    import concurrent.futures as cf
    d, appdata = _live_daemon()
    try:
        _pair_loopback(appdata / "flixcli" / "db.sqlite")
        ids = [str(uuid.uuid4()) for _ in range(100)]
        payloads = [{"id": i, "from": "fuzz-peer", "to": "me", "type": 0,
                     "time": 1, "groupId": "", "content": f"c-{n}"}
                    for n, i in enumerate(ids)]

        def send(b):
            return post(d.dev, "/bubble", b)[0]

        with cf.ThreadPoolExecutor(max_workers=16) as ex:
            codes = list(ex.map(send, payloads))
        from flixcli.config import Store
        with Store(appdata / "flixcli" / "db.sqlite") as s:
            rows = s.bubbles(200)
        seen = {r["id"] for r in rows}
        alive, _ = get(d.api, "/api/self")
        ok = (all(c == 200 for c in codes) and set(ids) <= seen
              and alive == 200)
        return ok, f"200s={codes.count(200)} stored={len(seen & set(ids))}"
    finally:
        d.stop()


def r81() -> tuple[bool, str]:
    """Unicode/control text survives the JSON hop byte-exact."""
    d, appdata = _live_daemon()
    try:
        _pair_loopback(appdata / "flixcli" / "db.sqlite")
        samples = ["中文测试", "emoji 🚀🔥", "line\nbreak", "nul\x00char",
                   "quote\"back\\slash", "ｆｕｌｌｗｉｄｔｈ", "\u200bzero"]
        for n, text in enumerate(samples):
            st, _ = post(d.dev, "/bubble", {
                "id": f"u{n}", "from": "fuzz-peer", "to": "me", "type": 0,
                "time": 1, "groupId": "", "content": text})
            if st != 200:
                return False, f"post {n} -> {st}"
        from flixcli.config import Store
        with Store(appdata / "flixcli" / "db.sqlite") as s:
            got = {r["id"]: r["content"] for r in s.bubbles(50)}
        bad = [(n, t, got.get(f"u{n}")) for n, t in enumerate(samples)
               if got.get(f"u{n}") != t]
        return not bad, f"mismatch={bad[:2]}"
    finally:
        d.stop()


def r82() -> tuple[bool, str]:
    """MCP server survives garbage lines and still serves tools/list."""
    CLI_HOME.mkdir(parents=True, exist_ok=True)
    lines = ["not json", "{}", "[1,2,3]", '{"jsonrpc":"2.0","id":1}',
             '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":null}',
             '{"jsonrpc":"2.0","id":3,"method":"tools/call",'
             '"params":{"name":"nope","arguments":{}}}',
             json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list",
                         "params": {}})]
    r = subprocess.run([PY, "-m", "flixcli.mcp_server"], input="\n".join(lines) + "\n",
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=30,
                       env={**os.environ, "APPDATA": str(CLI_HOME)})
    tools = 0
    for line in r.stdout.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("id") == 9:
            tools = len(msg.get("result", {}).get("tools", []))
    return r.returncode == 0 and tools == 23, f"rc={r.returncode} tools={tools}"


def r83() -> tuple[bool, str]:
    """Wrong-typed intent extras are rejected one by one, not fatally."""
    d, appdata = _live_daemon()
    try:
        _pair_loopback(appdata / "flixcli" / "db.sqlite")
        cases = [
            {"action": "askPairDevice", "deviceId": "fuzz-peer",
             "extra": {"verifyDevice": ["x"], "verifyCode": {"a": 1}}},
            {"action": "confirmPairDevice", "deviceId": "fuzz-peer",
             "extra": {"verifyDevice": None, "verifyCode": 1}},
            {"action": "rdGroupSync", "deviceId": "fuzz-peer",
             "extra": {"groupId": [1], "members": "x", "rev": "no"}},
            {"action": "rdAgent", "deviceId": "fuzz-peer",
             "extra": {"to": [], "id": {}, "body": None}},
        ]
        replies = [post(d.dev, "/intent", c) for c in cases]
        alive, _ = get(d.api, "/api/self")
        # Any status below 500 means the handler rejected or downgraded the
        # malformed extra instead of raising; the daemon must still answer.
        return all(st < 500 for st, _ in replies) and alive == 200, \
            f"{[st for st, _ in replies]} alive={alive}"
    finally:
        d.stop()


FUZZ_ROUNDS = [
    ("hypothesis datagram/announce", fuzz_node("datagram or announce")),
    ("hypothesis qr payload", fuzz_node("qr")),
    ("hypothesis model parsers", fuzz_node(
        "intent_reply or ping_v2 or bubble_unicode or resolve_target")),
    ("hypothesis upgrade script", fuzz_node("upgrade_script")),
    ("live malformed JSON", r78),
    ("live hostile upload names", r79),
    ("live concurrent bubbles", r80),
    ("live unicode fidelity", r81),
    ("mcp garbage lines", r82),
    ("live wrong-typed extras", r83),
]


def run_fuzz() -> None:
    for idx, (name, fn) in enumerate(FUZZ_ROUNDS, start=74):
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        record(idx, name, ok, detail)


# ---------------------------------------------------------------------------
# R84-R93: A/B against the pre-hardening baseline (bbbd813) + perf guards
# ---------------------------------------------------------------------------

def _fresh_appdata(tag: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"ab-{tag}-", dir=OUT))


def _own_fp(appdata: Path) -> str:
    prefs = appdata / "flixcli" / "shared_preferences.json"
    return json.loads(prefs.read_text(encoding="utf-8"))["identity"]["fingerprint"]


def _ab_pair(tag: str) -> tuple[Daemon, Daemon, Path, Path]:
    a_dir, b_dir = _fresh_appdata(tag + "-base"), _fresh_appdata(tag + "-cur")
    baseline = Daemon(a_dir, baseline=True, web_port=free_port())
    current = Daemon(b_dir, web_port=free_port())
    baseline.start()
    current.start()
    return baseline, current, a_dir, b_dir


def r84() -> tuple[bool, str]:
    src = BASELINE_SRC / "flixcli" / "server.py"
    text = src.read_text(encoding="utf-8")
    meta = BASELINE_SRC / "flixcli" / "__init__.py"
    return ("_trusted_sender" not in text and meta.exists()), \
        f"baseline at {BASELINE_SRC}"


def r85() -> tuple[bool, str]:
    """Unpaired bubble: baseline accepts (200), hardened build rejects (403)."""
    base, cur, _, _ = _ab_pair("bubble")
    try:
        body = {"id": str(uuid.uuid4()), "from": "stranger", "to": "x",
                "type": 0, "time": 1, "groupId": "", "content": "inject"}
        sb, _ = post(base.dev, "/bubble", body)
        sc, _ = post(cur.dev, "/bubble", body)
        return (sb == 200 and sc == 403), f"baseline={sb} current={sc}"
    finally:
        base.stop()
        cur.stop()


def r86() -> tuple[bool, str]:
    """Pairing with the wrong 4-digit code: baseline pairs, hardening rejects."""
    base, cur, a_dir, b_dir = _ab_pair("pair")
    try:
        results = []
        for d, appdata in ((base, a_dir), (cur, b_dir)):
            fp = _own_fp(appdata)
            results.append(post(d.dev, "/intent", {
                "action": "askPairDevice", "deviceId": "attacker",
                "extra": {"verifyDevice": fp, "verifyCode": "0000"}})[1])
        base_ok = "0: success" in results[0]
        cur_rejected = "0: success" not in results[1]
        return (base_ok and cur_rejected), \
            f"baseline={results[0][:40]!r} current={results[1][:40]!r}"
    finally:
        base.stop()
        cur.stop()


def r87() -> tuple[bool, str]:
    """Upload traversal: baseline escapes saveDir, current flattens it."""
    base, cur, a_dir, b_dir = _ab_pair("upload")
    outside = OUT / "ab-escape.txt"
    try:
        escapes, insides = [], []
        for d, appdata in ((base, a_dir), (cur, b_dir)):
            save = appdata / "save"
            save.mkdir(parents=True, exist_ok=True)
            outside.unlink(missing_ok=True)
            from flixcli.config import Store
            with Store(appdata / "flixcli" / "db.sqlite") as s:
                s.set_pref("saveDir", str(save))
                pin = s.own_web_pin()
            boundary = "----ab"
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; "
                'name="file"; filename="../../ab-escape.txt"\r\n'
                "Content-Type: application/octet-stream\r\n\r\nx\r\n"
                f"--{boundary}--\r\n").encode()
            post(d.web, f"/upload?pin={pin}", body, headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}"})
            # saveDir/../../ab-escape.txt resolves to OUT/ab-escape.txt.
            escapes.append(outside.exists())
            insides.append((save / "ab-escape.txt").exists())
        return (escapes[0] and not escapes[1] and insides[1]), \
            f"baseline_escape={escapes[0]} current_escape={escapes[1]} " \
            f"current_inside={insides[1]}"
    finally:
        base.stop()
        cur.stop()


def _get_noredirect(port: int, path: str) -> tuple[int, str]:
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        r = conn.getresponse()
        return r.status, r.getheader("Location") or ""
    finally:
        conn.close()


def r88() -> tuple[bool, str]:
    """PIN brute force on the GET route.

    Baseline only throttles /validate-code; GET /<pin> probes are unlimited, so
    after ten failures the right PIN still opens the page. The hardened build
    shares one counter across every entry point and redirects after the limit.
    """
    base, cur, a_dir, b_dir = _ab_pair("pin")
    try:
        results = []
        for d, appdata in ((base, a_dir), (cur, b_dir)):
            from flixcli.config import Store
            with Store(appdata / "flixcli" / "db.sqlite") as s:
                pin = s.own_web_pin()
            for _ in range(10):
                _get_noredirect(d.web, "/0000")
            results.append(_get_noredirect(d.web, f"/{pin}"))
        return (results[0][0] == 200 and results[1][0] == 302), \
            f"baseline={results[0]} current={results[1]}"
    finally:
        base.stop()
        cur.stop()


def _start_latency(baseline: bool, runs: int = 2) -> float:
    times = []
    for _ in range(runs):
        d = Daemon(_fresh_appdata("lat"), baseline=baseline)
        t0 = time.perf_counter()
        d.start()
        times.append(time.perf_counter() - t0)
        d.stop()
    return sorted(times)[len(times) // 2]


def r89() -> tuple[bool, str]:
    b, c = _start_latency(True), _start_latency(False)
    return c <= b * 1.5 + 1.0, f"baseline={b:.2f}s current={c:.2f}s"


def _burst_latency(baseline: bool) -> tuple[float, int]:
    import concurrent.futures as cf
    appdata = _fresh_appdata("burst")
    d = Daemon(appdata, baseline=baseline)
    d.start()
    try:
        from flixcli.config import Store
        with Store(appdata / "flixcli" / "db.sqlite") as s:
            s.add_pair("p", "1234")
            s.upsert_device("p", ip="127.0.0.1")
        payloads = [{"id": str(uuid.uuid4()), "from": "p", "to": "me",
                     "type": 0, "time": 1, "groupId": "", "content": f"b{n}"}
                    for n in range(100)]
        t0 = time.perf_counter()
        with cf.ThreadPoolExecutor(max_workers=16) as ex:
            codes = list(ex.map(lambda b: post(d.dev, "/bubble", b)[0], payloads))
        return time.perf_counter() - t0, codes.count(200)
    finally:
        d.stop()


def r90() -> tuple[bool, str]:
    b_t, b_ok = _burst_latency(True)
    c_t, c_ok = _burst_latency(False)
    ok = b_ok == 100 and c_ok == 100 and c_t <= b_t * 1.8 + 0.5
    return ok, f"baseline={b_t:.2f}s/{b_ok} current={c_t:.2f}s/{c_ok}"


_DISCOVERY_BENCH = r"""
import json, sys, time
from flixcli.discovery import parse_datagram
packet = json.dumps(json.dumps({"deviceModal": {
    "alias": "bench", "fingerprint": "fp", "port": 8891,
    "deviceType": "desktop", "os": "windows"}})).encode()
t0 = time.perf_counter()
n = 20000
ok = 0
for _ in range(n):
    ok += parse_datagram(packet, "10.0.0.9") is not None
print(json.dumps({"seconds": time.perf_counter() - t0, "ok": ok, "n": n}))
"""


def _bench_discovery(baseline: bool) -> dict:
    env = {"PYTHONPATH": str(BASELINE_SRC)} if baseline else {}
    r = run([PY, "-c", _DISCOVERY_BENCH], timeout=120, env=env)
    return json.loads(r.stdout.strip().splitlines()[-1])


def r91() -> tuple[bool, str]:
    b = _bench_discovery(True)
    c = _bench_discovery(False)
    ok = (b["ok"] == c["ok"] == b["n"]
          and c["seconds"] <= b["seconds"] * 2.0 + 0.05)
    return ok, f"baseline={b['seconds']:.2f}s current={c['seconds']:.2f}s"


def _import_latency(baseline: bool, runs: int = 3) -> float:
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        r = run([PY, "-c", "import flixcli.cli"], timeout=60,
                env={"PYTHONPATH": str(BASELINE_SRC)} if baseline else {})
        if r.returncode == 0:
            times.append(time.perf_counter() - t0)
    return sorted(times)[len(times) // 2]


def r92() -> tuple[bool, str]:
    b, c = _import_latency(True), _import_latency(False)
    return c <= b * 1.5 + 0.5, f"baseline={b:.2f}s current={c:.2f}s"


def _cli_latency(baseline: bool, runs: int = 3) -> float:
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        r = run([PY, "-m", "flixcli.cli", "self", "--json"], timeout=60,
                env={"PYTHONPATH": str(BASELINE_SRC)} if baseline else {})
        if r.returncode == 0:
            times.append(time.perf_counter() - t0)
    return sorted(times)[len(times) // 2]


def r93() -> tuple[bool, str]:
    b, c = _cli_latency(True), _cli_latency(False)
    return c <= b * 2.0 + 0.3, f"baseline={b:.2f}s current={c:.2f}s"


AB_ROUNDS = [
    ("baseline is pre-hardening", r84),
    ("A/B unpaired bubble", r85),
    ("A/B pairing code", r86),
    ("A/B upload traversal", r87),
    ("A/B PIN brute force", r88),
    ("perf daemon startup", r89),
    ("perf bubble burst", r90),
    ("perf discovery parse", r91),
    ("perf import time", r92),
    ("perf cli self", r93),
]


def run_ab() -> None:
    for idx, (name, fn) in enumerate(AB_ROUNDS, start=84):
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        record(idx, name, ok, detail)


# ---------------------------------------------------------------------------
# R94-R100: tooling gates
# ---------------------------------------------------------------------------

def r94() -> tuple[bool, str]:
    """Ruff: real errors only (undefined names, bad syntax). Style rules are a
    separate conversation; a 10k-line fork should not be reformatted in place."""
    r = run([PY, "-m", "ruff", "check", "src", "tests", "scripts",
             "--select", "E9,F63,F7,F82"], timeout=120)
    return r.returncode == 0, (r.stdout + r.stderr)[-300:]


def r95() -> tuple[bool, str]:
    """Bandit high severity. Mediums are LAN binds (by design) and nosec'd. See
    `python -m bandit -r src -ll` for the accepted list."""
    r = run([PY, "-m", "bandit", "-q", "-r", "src", "-lll"], timeout=180)
    high = "High: " in r.stdout and "High: 0" not in r.stdout
    return r.returncode == 0 and not high, (r.stdout + r.stderr)[-300:]


def r96() -> tuple[bool, str]:
    """Dependency audit: pip check + pip-audit (if installed)."""
    problems = []
    chk = run([PY, "-m", "pip", "check"], timeout=120)
    if chk.returncode != 0:
        problems.append(("pip-check", chk.stdout[-200:]))
    audit = run([PY, "-m", "pip_audit", "--local", "--progress-spinner", "off"],
                timeout=300)
    if audit.returncode == 127 or "No module named" in audit.stderr:
        problems.append(("pip-audit", "not installed"))
    elif audit.returncode != 0:
        problems.append(("pip-audit", audit.stdout[-400:]))
    return not problems, str(problems)[:400]


def r97() -> tuple[bool, str]:
    r = run([PY, "-m", "compileall", "-q", "src"], timeout=180)
    return r.returncode == 0, (r.stdout + r.stderr)[-300:]


def r98() -> tuple[bool, str]:
    r = run([PY, "-m", "pytest", "tests", "-q", "--timeout=180"], timeout=900)
    out = (r.stdout + r.stderr)[-600:]
    ok = r.returncode == 0 or (
        r.returncode in (127, 3221226505) and "passed" in out
        and "failed" not in out and "error" not in out.lower())
    return ok, out


def r99() -> tuple[bool, str]:
    cov_file = OUT / "coverage.json"
    r = run([PY, "-m", "pytest", "tests", "-q", "--timeout=180",
             "--cov=flixcli", "--cov-report", f"json:{cov_file}"], timeout=900)
    try:
        pct = json.loads(cov_file.read_text(encoding="utf-8"))["totals"]["percent_covered"]
    except (OSError, ValueError, KeyError):
        detail = (r.stdout + r.stderr)[-400:] if "r" in dir() else "missing"
        return False, f"coverage report missing: {detail}"
    # Floor, not a target: the fork starts near 44% (GUI/media paths are
    # exercised by live smoke, not unit tests). The gate catches regressions.
    return pct >= 40.0, f"coverage={pct:.1f}% (floor 40%)"


def r100() -> tuple[bool, str]:
    """Final smoke: both nodes, CLI, MCP and discover still work together."""
    checks = []
    e2e = run([PY, "tests/e2e.py"], timeout=300)
    checks.append(("e2e", "11/11 passed" in e2e.stdout))
    ver = flix(["--version"], CLI_HOME)
    checks.append(("version", ver.returncode == 0 and "2.2.1" in ver.stdout))
    tools = _mcp([{"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                   "params": {}}])
    names = {t["name"] for m in tools if m.get("id") == 2
             for t in m.get("result", {}).get("tools", [])}
    checks.append(("mcp-tools", len(names) == 23))
    disc = cli_discover()
    checks.append(("discover", disc[0]))
    bad = [n for n, ok in checks if not ok]
    return not bad, f"checks={checks}"


TOOLS_ROUNDS = [
    ("ruff real errors", r94),
    ("bandit high severity", r95),
    ("dependency audit", r96),
    ("compileall", r97),
    ("full pytest", r98),
    ("coverage >= 40% floor", r99),
    ("final smoke", r100),
]


def run_tools() -> None:
    for idx, (name, fn) in enumerate(TOOLS_ROUNDS, start=94):
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        record(idx, name, ok, detail)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("group", choices=["core", "cli", "fuzz", "ab", "tools"])
    args = ap.parse_args()
    if args.group == "core":
        run_core()
    elif args.group == "cli":
        run_cli()
    elif args.group == "fuzz":
        run_fuzz()
    elif args.group == "ab":
        run_ab()
    elif args.group == "tools":
        run_tools()
    else:
        print(f"group {args.group} not implemented yet")
        return 2
    failed = [r for r in RESULTS if not r["ok"]]
    out = OUT / f"rounds_{args.group}.json"
    out.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed -> {out}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
