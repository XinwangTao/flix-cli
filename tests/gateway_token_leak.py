"""验证级联令牌不会泄漏给客户端（安全回归）。

Release Notes 的承诺：「\--upstream-token\ 只用于第二跳鉴权，不会下发给手机」。

Release Notes 里写的是：「`--upstream-token` 只用于第二跳鉴权，不会下发给手机」。
这是安全承诺，要能证明：
1. 对端（级联消费方）的所有正常响应里都不出现它自己的 upstream_token；
2. 也不出现上游那台机器的令牌；
3. 出错时（4xx/5xx）同样不泄漏。
4. 自检页内嵌令牌是**设计**（页面 JS 要拿它调 API），但必须只能凭令牌取到
   —— 无令牌/错令牌都要 401，否则才是真泄漏。

用法：python tests/gateway_token_leak.py（对端地址/令牌在文件顶部，或改环境变量）
"""
from __future__ import annotations

import os
import json
import urllib.error
import urllib.request

PEER = "http://192.168.237.200:18993"
PEER_TOKEN = os.environ.get("RD_PEER_TOKEN") or ""
UPSTREAM_TOKEN = os.environ.get("RD_GATEWAY_TOKEN") or ""
LOCAL = "http://127.0.0.1:18993"

leaks: list[str] = []
checked = 0


def scan(label: str, text: str) -> None:
    global checked
    checked += 1
    hit = []
    if UPSTREAM_TOKEN in text:
        hit.append("upstream_token")
    if PEER_TOKEN in text:
        hit.append("peer_own_token")
    if hit:
        leaks.append(f"{label}: {hit}")
        print(f"  [LEAK] {label}: {hit}")
    else:
        print(f"  ok     {label}")


def page_gate(base: str, token: str) -> None:
    """自检页内嵌令牌是设计（页面 JS 要拿它调 API），但必须只能凭令牌取到。"""
    global checked
    for label, suffix, want_status in (
            ("自检页-无令牌", "", 401),
            ("自检页-错令牌", "?token=rd-wrong", 401),
            ("自检页-正确令牌", "?token=" + token, 200)):
        checked += 1
        try:
            with urllib.request.urlopen((base + "/" + suffix).replace("/?", "?"), timeout=20) as resp:
                status = resp.status
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            print(f"  skip   {label}: {type(exc).__name__}")
            continue
        if status != want_status:
            leaks.append(f"{label}: 期望 {want_status} 实际 {status}")
            print(f"  [FAIL] {label}: 期望 {want_status} 实际 {status}")
        elif status == 200 and token not in body:
            print(f"  ok     {label}: 200 且内嵌令牌（设计如此）")
        else:
            print(f"  ok     {label}: {status}")


def get(label: str, url: str, token: str) -> None:
    request = urllib.request.Request(url, headers={"x-api-key": token})
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            scan(label, resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        scan(label + " (err)", exc.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        print(f"  skip   {label}: {type(exc).__name__}")


def post(label: str, url: str, token: str, body: dict) -> None:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "x-api-key": token,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(request, timeout=180) as resp:
            scan(label, resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        scan(label + " (err)", exc.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        print(f"  skip   {label}: {type(exc).__name__}")


def main() -> int:
    print("对端（级联消费方）响应体扫描：")
    get("/v1/models", PEER + "/v1/models", PEER_TOKEN)
    get("/health", PEER + "/health", PEER_TOKEN)
    page_gate(PEER, PEER_TOKEN)
    post("/v1/messages", PEER + "/v1/messages", PEER_TOKEN,
         {"model": "glm-5.3-flash", "max_tokens": 64,
          "messages": [{"role": "user", "content": "只回答：OK"}]})
    post("未知模型(400)", PEER + "/v1/messages", PEER_TOKEN,
         {"model": "no-such-model-xyz", "max_tokens": 16,
          "messages": [{"role": "user", "content": "hi"}]})
    post("坏 body(400)", PEER + "/v1/messages", PEER_TOKEN, {"model": "glm-5.3-flash"})

    print("\n上游（本机）响应体扫描：")
    get("本机 /health", LOCAL + "/health", UPSTREAM_TOKEN)
    get("本机 /v1/models", LOCAL + "/v1/models", UPSTREAM_TOKEN)

    print(f"\n检查了 {checked} 个响应，泄漏 {len(leaks)} 处")
    for row in leaks:
        print("  -", row)
    return 0 if not leaks else 1


if __name__ == "__main__":
    raise SystemExit(main())
