"""两台真实设备并发：手机直连 + 一台走级联的机器，同时打同一个网关。

之前测过「多线程客户端 + 手机」，但那些客户端只是本机线程，不是第二台真实设备。
这个脚本用「级联消费方」（真机、经中间网关）与「手机」（真机、直连）同时发请求，
覆盖真实布局下「两个用户同时用」的情况。

用法（需要两台设备都在线；手机那侧靠 rdProbe 深链，没有 adb 设备会记为失败）：
    python tests/gateway_two_real_devices.py

必要时用环境变量覆盖地址与令牌：
    RD_LOCAL_TOKEN / RD_PEER_URL / RD_PEER_TOKEN
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.request

import os

LOCAL = os.environ.get("RD_LOCAL_URL") or "http://127.0.0.1:18993"
LOCAL_TOKEN = os.environ.get("RD_LOCAL_TOKEN") or ""
PEER = os.environ.get("RD_PEER_URL") or "http://192.168.237.200:18993"
PEER_TOKEN = os.environ.get("RD_PEER_TOKEN") or ""
PROMPT = "只回答两个字：通了"
ROUNDS = 3


def one_request(base: str, token: str, path: str) -> dict:
    """path: messages | chat"""
    openai = path == "chat"
    endpoint = "/v1/chat/completions" if openai else "/v1/messages"
    body = json.dumps({"model": "glm-5.3-flash", "max_tokens": 200,
                       "messages": [{"role": "user", "content": PROMPT}]}).encode()
    headers = {"content-type": "application/json"}
    if openai:
        headers["authorization"] = f"Bearer {token}"
    else:
        headers["x-api-key"] = token
        headers["anthropic-version"] = "2023-06-01"
    request = urllib.request.Request(base + endpoint, data=body, headers=headers)
    began = time.time()
    try:
        with urllib.request.urlopen(request, timeout=180) as resp:
            payload = json.loads(resp.read())
        if openai:
            message = ((payload.get("choices") or [{}])[0] or {}).get("message") or {}
            text = (message.get("content") or "").strip()
            think = (message.get("reasoning_content") or "").strip()
        else:
            blocks = payload.get("content") or []
            text = "".join(b.get("text", "") for b in blocks
                           if b.get("type") == "text").strip()
            think = "".join(b.get("thinking", "") for b in blocks
                            if b.get("type") == "thinking").strip()
        ok = bool(text or think)
        label = text[:8] if text else (f"(思考{len(think)}字)" if think else "(空)")
        return {"ok": ok, "ms": int((time.time() - began) * 1000), "label": label}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "ms": int((time.time() - began) * 1000),
                "label": type(exc).__name__}


def peer_worker(results: list) -> None:
    for index in range(ROUNDS):
        path = "chat" if index % 2 else "messages"
        results.append(("peer", path, one_request(PEER, PEER_TOKEN, path)))


def phone_worker(results: list) -> None:
    def adb(*args: str, timeout: int = 200) -> str:
        out = subprocess.run(["adb", "-s", "c122c21c", *args],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=timeout)
        return (out.stdout or "") + (out.stderr or "")

    for _ in range(ROUNDS):
        adb("logcat", "-c")
        adb("shell", "am", "force-stop", "com.relaydesk.app.debug")
        time.sleep(1.0)
        adb("shell", "am", "start", "-n",
            "com.relaydesk.app.debug/com.relaydesk.app.MainActivity",
            "-e", "rdProbe", "gateway")
        deadline = time.time() + 150
        done = False
        while time.time() < deadline:
            time.sleep(3)
            log = adb("logcat", "-d", "-s", "RelayDeskProbe")
            for line in log.splitlines():
                if "completion:" in line:
                    detail = line.split("completion:", 1)[1].strip()
                    ok = "补全成功" in detail
                    results.append(("phone", "messages", {"ok": ok, "ms": 0,
                                                          "label": detail[:60]}))
                    done = True
                    break
            if done:
                break
        if not done:
            results.append(("phone", "messages",
                            {"ok": False, "ms": 0, "label": "超时无日志"}))


def main() -> int:
    results: list = []
    threads = [threading.Thread(target=peer_worker, args=(results,)),
               threading.Thread(target=phone_worker, args=(results,))]
    began = time.time()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.time() - began

    for who, path, row in results:
        mark = "OK  " if row["ok"] else "FAIL"
        print(f"  {who:<5} {path:<9} {mark} {row['ms']:>6}ms {row['label']}")
    passed = sum(1 for _, _, row in results if row["ok"])
    print(f"\n合计 {passed}/{len(results)} 成功，总耗时 {elapsed:.1f}s")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
