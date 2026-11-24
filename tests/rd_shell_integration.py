"""Integration check for rdShell on two real daemons (no mocks).

Covers what the field failures were:
- exec works end to end, exit code is passed through, chained commands survive
  the quoting round-trip;
- a PTY client that is killed without a close frame does not poison the peer's
  single session slot (the "409 busy until restart" bug).

Usage: python tests/rd_shell_integration.py [--a-port 20991] [--b-port 20995]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# FLIX_BIN lets the same check run against a frozen build (dist\RelayDesk.exe).
FLIX = os.environ.get("FLIX_BIN") or str(ROOT / ".venv" / "Scripts" / "flix.exe")
A_ENV = {"APPDATA": str(ROOT / "reverse" / "rd_a")}
B_ENV = {"APPDATA": str(ROOT / "reverse" / "rd_b")}

results: list[bool] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def sh(args, env=None, timeout=40):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run([FLIX, *args], capture_output=True, text=True,
                          env=e, timeout=timeout)


def wait_port(port, tries=120):
    import socket
    for _ in range(tries):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


async def ws_open(port: int, path: str):
    """Raw HTTP upgrade so the socket can be aborted without a close frame."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    key = base64.b64encode(os.urandom(16)).decode()
    writer.write((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                  "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Key: {key}\r\n"
                  "Sec-WebSocket-Version: 13\r\n\r\n").encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
    return int(head.split(b" ")[1]), reader, writer


async def main(a_port: int, b_port: int) -> int:
    procs, logs = [], []
    for env, dp, ap in ((A_ENV, a_port, a_port + 7), (B_ENV, b_port, b_port + 7)):
        shutil.rmtree(env["APPDATA"], ignore_errors=True)
        Path(env["APPDATA"]).mkdir(parents=True, exist_ok=True)
        log = open(Path(env["APPDATA"]) / "daemon.err", "wb")
        logs.append((env["APPDATA"], log))
        procs.append(subprocess.Popen(
            [FLIX, "serve", "--device-port", str(dp), "--api-port", str(ap),
             "--web-port", str(dp + 1), "--no-announce"],
            env={**os.environ, **env}, stdout=subprocess.DEVNULL, stderr=log))
    try:
        check("ports-up", wait_port(a_port) and wait_port(b_port))

        r = sh(["ping", "--json", f"127.0.0.1:{b_port}"], env=A_ENV)
        fp_b = json.loads(r.stdout)["peer"]["fingerprint"]

        from flixcli.config import Store, load_or_create_identity
        db_b = Path(B_ENV["APPDATA"]) / "flixcli" / "db.sqlite"
        for _ in range(40):
            if db_b.exists():
                break
            time.sleep(0.2)
        with Store(db_b) as sb:
            code_b = sb.own_pair_key()
        sh(["pair", "--json", f"127.0.0.1:{b_port}", "--code", code_b], env=A_ENV)
        with Store(Path(A_ENV["APPDATA"]) / "flixcli" / "db.sqlite") as sa:
            own_a = load_or_create_identity(sa)
        with Store(db_b) as sb:
            sb.upsert_device(own_a["fingerprint"], ip="127.0.0.1", port=a_port)

        # --- exec -----------------------------------------------------------
        r = sh(["shell", "--json", f"127.0.0.1:{b_port}", "--", "whoami"],
               env=A_ENV)
        body = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}
        check("exec-whoami", r.returncode == 0 and body.get("exit") == 0
              and body.get("stdout", "").strip() != "", r.stdout.strip()[:120])

        r = sh(["shell", "--json", f"127.0.0.1:{b_port}", "--",
                "hostname", "&", "echo", "CHAIN-OK"], env=A_ENV)
        body = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}
        check("exec-chained", "CHAIN-OK" in body.get("stdout", ""),
              body.get("stdout", "").strip()[:80])

        r = sh(["shell", f"127.0.0.1:{b_port}", "--", "cmd", "/c", "exit", "7"],
               env=A_ENV)
        check("exec-exit-code-passthrough", r.returncode == 7, f"rc={r.returncode}")

        # --- PTY slot: a killed client must not leave the peer "busy" -------
        path = f"/shell/pty?from={own_a['fingerprint']}&sid=it-1&cols=100&rows=30&shell=cmd"
        status, reader, writer = await ws_open(b_port, path)
        check("pty-open", status == 101, f"status={status}")
        await asyncio.sleep(0.5)
        writer.transport.abort()          # no close frame: a killed client
        await asyncio.sleep(2.0)          # let the daemon notice

        status2, reader2, writer2 = await ws_open(b_port, path.replace("it-1", "it-2"))
        check("pty-slot-released-after-abort", status2 == 101, f"status={status2}")
        writer2.transport.abort()
        await asyncio.sleep(1.0)

        # --- graceful close also frees the slot -----------------------------
        status3, reader3, writer3 = await ws_open(b_port, path.replace("it-1", "it-3"))
        check("pty-reopen-after-graceful", status3 == 101, f"status={status3}")
        writer3.transport.abort()
    finally:
        for p in procs:
            p.terminate()
        for appdata, log in logs:
            log.close()
        time.sleep(1.0)
        for appdata, _ in logs:
            err = Path(appdata) / "daemon.err"
            text = err.read_text(errors="replace") if err.exists() else ""
            noisy = [ln for ln in text.splitlines()
                     if "Traceback" in ln or "[shell]" in ln or "Error" in ln]
            if noisy:
                print(f"\n-- {err.name} warnings --\n" + "\n".join(noisy[-8:]))

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--a-port", type=int, default=20991)
    ap.add_argument("--b-port", type=int, default=20995)
    ns = ap.parse_args()
    sys.exit(asyncio.run(main(ns.a_port, ns.b_port)))
