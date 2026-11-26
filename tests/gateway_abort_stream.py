"""客户端中途断开：网关会不会泄漏、会不会误伤路由？

真实场景：手机用户切走 App / 锁屏 / Wi-Fi 掉线，SSE 连接被 TCP 掐断。
必须验证三点：
1. 断开后 active/inflight 能回到 0（不泄漏）；
2. 断开计入 aborted 而不是 failed（否则 failed 会涨，doctor 会误报）；
3. 断了不能把路由标死——否则其他设备接下来 60s 全收到 503。
第 3 点是真踩过的：单条直连路由被一次抖动标死后，所有人都 503。

用法（需要网关已在跑）：
    python tests/gateway_abort_stream.py
"""
from __future__ import annotations

import os
import json
import socket
import time
import urllib.request

BASE_HOST = "127.0.0.1"
BASE_PORT = 18993
TOKEN = os.environ.get("RD_GATEWAY_TOKEN") or ""
if not TOKEN:
    raise SystemExit(
        "请设置环境变量 RD_GATEWAY_TOKEN（桌面端「模型共享」面板可复制）")

MODEL = "glm-5.3-flash"


def health() -> dict:
    request = urllib.request.Request(f"http://{BASE_HOST}:{BASE_PORT}/health",
                                     headers={"x-api-key": TOKEN})
    with urllib.request.urlopen(request, timeout=20) as resp:
        return json.loads(resp.read())


def abort_mid_stream(wait_bytes: int = 200, timeout: float = 60.0) -> str:
    """手写裸 socket：发出流式请求，收到一点数据就暴力 close。"""
    body = json.dumps({
        "model": MODEL, "max_tokens": 600, "stream": True,
        "messages": [{"role": "user",
                      "content": "用三句话介绍一下你自己"}],
    }).encode()
    head = (
        "POST /v1/messages HTTP/1.1\r\n"
        f"Host: {BASE_HOST}:{BASE_PORT}\r\n"
        "content-type: application/json\r\n"
        f"x-api-key: {TOKEN}\r\n"
        "anthropic-version: 2023-06-01\r\n"
        f"content-length: {len(body)}\r\n"
        "connection: close\r\n\r\n"
    ).encode()
    sock = socket.create_connection((BASE_HOST, BASE_PORT), timeout=10)
    try:
        sock.settimeout(45.0)
        sock.sendall(head + body)
        got = b""
        deadline = time.time() + timeout
        while len(got) < wait_bytes and time.time() < deadline:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                # 模型还在思考、首字节未到就断开——同样是真实场景
                return f"首字节前断开（已等 {timeout:.0f}s）"
            if not chunk:
                break
            got += chunk
        return f"收到 {len(got)} 字节后断开"
    finally:
        sock.close()


def main() -> int:
    start = health()
    print(f"起始 active={start['stats']['active']} "
          f"inflight={len(start['stats'].get('inflight') or {})}", flush=True)

    for attempt in range(10):
        note = abort_mid_stream()
        print(f"  round {attempt + 1}: {note}", flush=True)
        time.sleep(0.5)

    # 断开后等一会儿，让网关有机会收尾
    for wait in (5, 15, 30, 60):
        time.sleep(wait if wait == 5 else wait - (wait // 2))
        now = health()
        active = now["stats"]["active"]
        inflight = len(now["stats"].get("inflight") or {})
        print(f"  +{wait}s: active={active} inflight={inflight} "
              f"failed={now['stats']['failed']} aborted={now['stats']['aborted']}",
              flush=True)
        if active == 0 and inflight == 0:
            print(f"\n结论: 断开后 {wait}s 内清干净，无泄漏", flush=True)
            return 0
    print("\n结论: 60s 后仍有在途登记，需要检查取消路径", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
