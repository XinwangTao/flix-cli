#!/usr/bin/env python3
"""End-to-end regression for two isolated flixcli nodes (no official needed).

Usage:  python tests/e2e.py
Spawns node A (18991/18998) and node B (18995/18997) with isolated APPDATA,
then exercises: ping_v2, pair, send-text, send-file, group create/send,
web page auth, webrtc signaling, qrcode roundtrip. Exit 0 = all green.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FLIX = str(ROOT / ".venv" / "Scripts" / "flix.exe")
A = {"APPDATA": r"D:\flix-cli\reverse\reg_a"}
B = {"APPDATA": r"D:\flix-cli\reverse\reg_b"}
A_PORT, B_PORT = 18991, 18995


def sh(args, env=None, timeout=20):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run([FLIX, *args], capture_output=True, text=True,
                          env=e, timeout=timeout)


def http(method, url, body=None, timeout=6):
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode()


def wait_port(port, tries=30):
    import socket
    for _ in range(tries):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.4)
    return False


def main() -> int:
    ok = []

    def check(name, cond, detail=""):
        ok.append(cond)
        print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")

    procs = []
    for env, dp, ap in ((A, A_PORT, 18998), (B, B_PORT, 18997)):
        shutil.rmtree(env["APPDATA"], ignore_errors=True)
        p = subprocess.Popen([FLIX, "serve", "--device-port", str(dp),
                              "--api-port", str(ap), "--web-port", str(dp + 1),
                              "--no-announce"],
                             env={**os.environ, **env},
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
    try:
        check("ports-up", wait_port(A_PORT) and wait_port(B_PORT))

        r = sh(["ping", "--json", f"127.0.0.1:{B_PORT}"], env=A)
        check("ping_v2", r.returncode == 0 and '"ok": true' in r.stdout)
        fp_b = json.loads(r.stdout)["peer"]["fingerprint"]

        r = sh(["pair", "--json", f"127.0.0.1:{B_PORT}"], env=A)
        check("pair", r.returncode == 0 and '"paired": true' in r.stdout)
        # The isolated nodes communicate over loopback; align the test fixture
        # with the production source-IP binding instead of weakening the server.
        from flixcli.config import Store, load_or_create_identity
        with Store(Path(A["APPDATA"]) / "flixcli" / "db.sqlite") as store_a:
            own_a = load_or_create_identity(store_a)
        with Store(Path(B["APPDATA"]) / "flixcli" / "db.sqlite") as store_b:
            store_b.upsert_device(own_a["fingerprint"], ip="127.0.0.1", port=A_PORT)

        r = sh(["send-text", "--json", f"127.0.0.1:{B_PORT}", "e2e-hello"], env=A)
        check("send-text", r.returncode == 0 and '"ok": true' in r.stdout)

        file_name = f"e2e_file_{int(time.time() * 1000)}.txt"
        tf = ROOT / "reverse" / file_name
        tf.parent.mkdir(exist_ok=True)
        tf.write_text(f"e2e-{time.time()}", encoding="utf-8")
        r = sh(["send", "--json", f"127.0.0.1:{B_PORT}", str(tf)], env=A)
        check("send-file", r.returncode == 0 and '"ok": true' in r.stdout,
              f"stdout={r.stdout.strip()} stderr={r.stderr.strip()}")
        got = Path(os.path.expanduser("~")) / "Downloads" / file_name
        check("file-landed", got.exists())
        if got.exists():
            got.unlink()

        r = sh(["group", "create", "--json", "reg-group",
                "--member", fp_b], env=A)
        check("group-create", r.returncode == 0)

        # web: pin + upload
        pin_file = Path(env2 := B["APPDATA"]) / "flixcli" / "shared_preferences.json"
        pin = json.loads(pin_file.read_text(encoding="utf-8")).get("web_pin")
        st, body = http("POST", f"http://127.0.0.1:{B_PORT+1}/validate-code",
                        {"code": pin})
        check("web-validate", st == 200)

        # webrtc signaling
        st, body = http("POST", f"http://127.0.0.1:{B_PORT}/webrtc/signal",
                        {"from": "probe", "type": "offer", "callId": "e2e",
                         "payload": {"sdp": "x"}})
        check("webrtc-signal", st == 200 and json.loads(body)["ok"] is True)

        # qrcode roundtrip
        r = sh(["qrcode", "--json", "--decode"], env=A)
        check("qrcode", r.returncode == 0 and '"payload"' in r.stdout)

        # clipboard intent A -> B
        r = sh(["clipboard", "--json", f"127.0.0.1:{B_PORT}", "clip-e2e"], env=A)
        check("clipboard", r.returncode == 0 and "{0: success}" in r.stdout)
    finally:
        for p in procs:
            p.terminate()
        try:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
        except Exception:
            pass

    print(f"\n{sum(ok)}/{len(ok)} passed")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    sys.exit(main())
