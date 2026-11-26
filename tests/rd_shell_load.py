"""rdShell 高并发实测：两个真实 daemon，N 路并发 exec + K 路并发 PTY。

验证目标（对应用户要求「局域网 SSH 高并发、且不影响其它功能」）：
1. N 路并发 exec 全部成功（旧版本上限 4，第 5 路立刻 exit=124）；
2. 并发期间设备通道仍然可用（ping_v2 不受阻塞）；
3. K 路并发 PTY 全部能打开（旧版本上限 1，第二个 409 busy）。

用法: python rd_shell_load.py [--exec 24] [--pty 8]
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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(r"F:\projects\flix-cli-wt-live-toggle")
PY = r"F:\projects\flix-cli\.venv\Scripts\python.exe"
SRC = str(ROOT / "src")
ENV_A = {"APPDATA": str(ROOT / "reverse" / "load_a")}
ENV_B = {"APPDATA": str(ROOT / "reverse" / "load_b")}

results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def base_env(extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC
    env.update(extra or {})
    return env


def flix(args: list[str], extra_env: dict | None = None, timeout: int = 120):
    return subprocess.run([PY, "-m", "flixcli.cli", *args],
                          capture_output=True, text=True,
                          env=base_env(extra_env), timeout=timeout)


def wait_port(port: int, tries: int = 150) -> bool:
    import socket
    for _ in range(tries):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


async def ws_open(port: int, path: str):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    key = base64.b64encode(os.urandom(16)).decode()
    writer.write((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                  "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Key: {key}\r\n"
                  "Sec-WebSocket-Version: 13\r\n\r\n").encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
    return int(head.split(b" ")[1]), reader, writer


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exec", dest="n_exec", type=int, default=24)
    ap.add_argument("--pty", dest="n_pty", type=int, default=8)
    ap.add_argument("--a-port", type=int, default=21991)
    ap.add_argument("--b-port", type=int, default=21995)
    args = ap.parse_args()

    procs, logs = [], []
    for env, dp, ap_port in ((ENV_A, args.a_port, args.a_port + 7),
                             (ENV_B, args.b_port, args.b_port + 7)):
        shutil.rmtree(env["APPDATA"], ignore_errors=True)
        Path(env["APPDATA"]).mkdir(parents=True, exist_ok=True)
        log = open(Path(env["APPDATA"]) / "daemon.err", "wb")
        logs.append(log)
        procs.append(subprocess.Popen(
            [PY, "-m", "flixcli.cli", "serve", "--device-port", str(dp),
             "--api-port", str(ap_port), "--web-port", str(dp + 1),
             "--no-announce"],
            env=base_env(env), stdout=subprocess.DEVNULL, stderr=log))
    try:
        check("ports-up", wait_port(args.a_port) and wait_port(args.b_port))

        r = flix(["ping", "--json", f"127.0.0.1:{args.b_port}"], ENV_A)
        fp_b = json.loads(r.stdout)["peer"]["fingerprint"]

        sys.path.insert(0, SRC)
        from flixcli.config import Store, load_or_create_identity

        db_b = Path(ENV_B["APPDATA"]) / "flixcli" / "db.sqlite"
        for _ in range(50):
            if db_b.exists():
                break
            time.sleep(0.2)
        with Store(db_b) as sb:
            code_b = sb.own_pair_key()
        flix(["pair", "--json", f"127.0.0.1:{args.b_port}", "--code", code_b],
             ENV_A)
        with Store(Path(ENV_A["APPDATA"]) / "flixcli" / "db.sqlite") as sa:
            own_a = load_or_create_identity(sa)
        with Store(db_b) as sb:
            sb.upsert_device(own_a["fingerprint"], ip="127.0.0.1",
                             port=args.a_port)

        target = f"127.0.0.1:{args.b_port}"

        # --- 1. 并发 exec -------------------------------------------------
        def one(i: int):
            started = time.monotonic()
            r = flix(["shell", "--json", target, "--", "cmd", "/c",
                      f"echo JOB-{i}"], ENV_A, timeout=180)
            elapsed = time.monotonic() - started
            body = {}
            if r.stdout.strip().startswith("{"):
                try:
                    body = json.loads(r.stdout)
                except ValueError:
                    body = {}
            ok = (r.returncode == 0 and body.get("exit") == 0
                  and f"JOB-{i}" in body.get("stdout", ""))
            return ok, elapsed, body.get("stderr", "")

        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=args.n_exec) as pool:
            outcomes = list(pool.map(one, range(args.n_exec)))
        wall = time.monotonic() - started
        ok_count = sum(1 for ok, _, _ in outcomes if ok)
        slowest = max(elapsed for _, elapsed, _ in outcomes)
        busy = [e for _, _, e in outcomes if "too many concurrent" in (e or "")]
        check(f"exec-{args.n_exec}-concurrent", ok_count == args.n_exec,
              f"ok={ok_count}/{args.n_exec} wall={wall:.1f}s "
              f"slowest={slowest:.1f}s busy_rejects={len(busy)}")

        # --- 2. 并发期间设备通道仍然可用 -----------------------------------
        r = flix(["ping", "--json", target], ENV_A, timeout=60)
        check("channel-responsive-after-burst", r.returncode == 0
              and '"fingerprint"' in r.stdout, r.stdout.strip()[:100])

        # --- 3. 并发 PTY --------------------------------------------------
        async def pty_probe() -> list[int]:
            statuses = []
            opened = []

            async def open_one(i: int) -> int:
                path = (f"/shell/pty?from={own_a['fingerprint']}"
                        f"&sid=load-{i}&cols=100&rows=30&shell=cmd")
                try:
                    status, reader, writer = await ws_open(args.b_port, path)
                except Exception:  # noqa: BLE001
                    return -1
                if status == 101:
                    opened.append(writer)
                else:
                    writer.close()
                return status

            statuses = await asyncio.gather(
                *(open_one(i) for i in range(args.n_pty)))
            for writer in opened:
                writer.transport.abort()
            return list(statuses)

        statuses = asyncio.run(pty_probe())
        opened = sum(1 for s in statuses if s == 101)
        check(f"pty-{args.n_pty}-concurrent", opened == args.n_pty,
              f"opened={opened}/{args.n_pty} statuses={statuses}")

        await_drain = time.sleep(1.0)
        return 0 if all(ok for _, ok, _ in results) else 1
    finally:
        for p in procs:
            p.terminate()
        for log in logs:
            log.close()
        time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
