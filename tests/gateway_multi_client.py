"""多设备并发实测：多客户端同时打网关 + 手机同时跑，看会不会互相饿死。

真实现场是「手机 + 平板 + 同事电脑」一起用同一个网关。单元测试只覆盖单进程
语义，这个脚本覆盖「真实客户端混在一起」的组合。

用法（需要网关已在跑；手机那项可选，没有 adb 设备就跳过）：
    python tests/gateway_multi_client.py [客户端数] [每客户端请求数]

判定口径与 lix models doctor 一致：HTTP 200 且（有正文 或 有思考）即算通过
——推理模型在 max_tokens 小时只回思考是正常行为，不是网关故障。
"""
from __future__ import annotations

import os
import json
import subprocess
import sys
import threading
import time
import urllib.request

BASE = "http://127.0.0.1:18993"
TOKEN = os.environ.get("RD_GATEWAY_TOKEN") or ""
if not TOKEN:
    raise SystemExit(
        "请设置环境变量 RD_GATEWAY_TOKEN（桌面端「模型共享」面板可复制）")

MODEL = "glm-5.3-flash"

CLIENTS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
PER_CLIENT = int(sys.argv[2]) if len(sys.argv) > 2 else 2

stop = threading.Event()
lock = threading.Lock()
records: list[dict] = []


def one_call(client: int, proto: str, seq: int) -> dict:
    openai = proto == "openai"
    path = "/v1/chat/completions" if openai else "/v1/messages"
    body = {"model": MODEL, "max_tokens": 200,
            "messages": [{"role": "user", "content": "只回答两个字：通了"}]}
    headers = {"content-type": "application/json"}
    if openai:
        headers["authorization"] = f"Bearer {TOKEN}"
    else:
        headers["x-api-key"] = TOKEN
        headers["anthropic-version"] = "2023-06-01"
    request = urllib.request.Request(BASE + path,
                                     data=json.dumps(body).encode(),
                                     headers=headers)
    began = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=200) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        elapsed = time.monotonic() - began
        # 推理模型在 max_tokens 小时可能只回思考：链路是通的，算通过，
        # 但要在结果里标出来，别把模型行为当成网关故障（实测踩过）。
        if openai:
            msg = ((payload.get("choices") or [{}])[0] or {}).get("message") or {}
            text = (msg.get("content") or "").strip()
            reasoning = (msg.get("reasoning_content") or "").strip()
        else:
            text = "".join(b.get("text", "") for b in payload.get("content", [])
                           if b.get("type") == "text").strip()
            reasoning = "".join(b.get("thinking", "")
                                for b in payload.get("content", [])
                                if b.get("type") == "thinking").strip()
        ok = bool(text) or bool(reasoning)
        label = text[:12] if text else (f"(思考{len(reasoning)}字)" if reasoning else "(空)")
        return {"client": client, "proto": proto, "seq": seq, "ok": ok,
                "status": resp.status, "ms": int(elapsed * 1000),
                "text": label}
    except Exception as exc:  # noqa: BLE001
        return {"client": client, "proto": proto, "seq": seq, "ok": False,
                "status": 0, "ms": int((time.monotonic() - began) * 1000),
                "text": type(exc).__name__}


def client_worker(index: int) -> None:
    proto = "openai" if index % 2 else "anthropic"
    for seq in range(PER_CLIENT):
        record = one_call(index, proto, seq)
        with lock:
            records.append(record)
        if seq + 1 < PER_CLIENT:
            time.sleep(2)


def phone_probe() -> str:
    """在手机端同时跑一次 rdProbe（真实客户端，非回环）。"""
    def adb(*args: str, timeout: int = 180) -> str:
        out = subprocess.run(["adb", "-s", "c122c21c", *args],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=timeout)
        return (out.stdout or "") + (out.stderr or "")

    adb("logcat", "-c")
    adb("shell", "am", "force-stop", "com.relaydesk.app.debug")
    time.sleep(1.0)
    adb("shell", "am", "start", "-n",
        "com.relaydesk.app.debug/com.relaydesk.app.MainActivity",
        "-e", "rdProbe", "gateway")
    deadline = time.time() + 150
    while time.time() < deadline:
        time.sleep(3)
        log = adb("logcat", "-d", "-s", "RelayDeskProbe")
        for line in log.splitlines():
            if "completion:" in line:
                return line.split("completion:", 1)[1].strip()[:110]
    return "（超时，没拿到日志）"


def main() -> int:
    print(f"{CLIENTS} 个客户端 × {PER_CLIENT} 次 + 手机端同时跑", flush=True)
    threads = [threading.Thread(target=client_worker, args=(i,), daemon=True)
               for i in range(CLIENTS)]
    began = time.monotonic()
    for thread in threads:
        thread.start()
    phone_box: list[str] = []

    def phone_worker() -> None:
        phone_box.append(phone_probe())

    phone_thread = threading.Thread(target=phone_worker, daemon=True)
    phone_thread.start()
    for thread in threads:
        thread.join()
    phone_thread.join()
    elapsed = time.monotonic() - began

    for rec in sorted(records, key=lambda r: (r["client"], r["seq"])):
        print(f"  client{rec['client']} {rec['proto']:<9} seq{rec['seq']} "
              f"{'OK ' if rec['ok'] else 'FAIL'} {rec['status']} "
              f"{rec['ms']:>6}ms {rec['text']}", flush=True)
    passed = sum(1 for rec in records if rec["ok"])
    phone_ok = bool(phone_box) and "补全成功" in phone_box[0]
    print(f"  手机端同时: {'OK' if phone_ok else 'FAIL'} "
          f"{phone_box[0] if phone_box else '（无结果）'}", flush=True)
    print(f"\n合计 {passed}/{len(records)} 成功，手机 {1 if phone_ok else 0}/1，"
          f"总耗时 {elapsed:.1f}s", flush=True)
    return 0 if passed == len(records) and phone_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
