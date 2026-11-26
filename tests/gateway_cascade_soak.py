"""级联路径持续可靠性：每隔 N 秒经对端的网关打一次真实补全。

一次性检查只能证明「那一下是通的」；现场关心的是「一两个小时里经过中间网关
的请求会不会开始出错」。这个脚本连续采样、统计成功率与耗时，并在首尾各读一次
对端的 /health，看 failed 有没有增长（能区分「我的请求失败」和「对端自己出错」）。

一次性检查只能证明「那一下是通的」；真实现场关心的是「一两个小时里
经过中间网关的请求会不会开始出错」。这里连续采样并统计。

用法（默认对端地址与令牌可用 RD_PEER_URL / RD_PEER_TOKEN 覆盖）:
    python tests/gateway_cascade_soak.py [分钟数] [间隔秒]
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

PEER = os.environ.get("RD_PEER_URL") or "http://192.168.237.200:18993"
TOKEN = os.environ.get("RD_PEER_TOKEN") or ""
if not TOKEN:
    raise SystemExit(
        "请设置环境变量 RD_GATEWAY_TOKEN（桌面端「模型共享」面板可复制）")

MINUTES = int(sys.argv[1]) if len(sys.argv) > 1 else 20
INTERVAL = int(sys.argv[2]) if len(sys.argv) > 2 else 60


def health() -> dict:
    request = urllib.request.Request(PEER + "/health",
                                     headers={"x-api-key": TOKEN})
    with urllib.request.urlopen(request, timeout=30) as resp:
        return json.loads(resp.read())


def completion(openai: bool) -> tuple[bool, float, str]:
    path = "/v1/chat/completions" if openai else "/v1/messages"
    body = json.dumps({"model": "glm-5.3-flash", "max_tokens": 200,
                       "messages": [{"role": "user",
                                     "content": "只回答两个字：通了"}]}).encode()
    headers = {"content-type": "application/json"}
    if openai:
        headers["authorization"] = f"Bearer {TOKEN}"
    else:
        headers["x-api-key"] = TOKEN
        headers["anthropic-version"] = "2023-06-01"
    request = urllib.request.Request(PEER + path, data=body, headers=headers)
    began = time.time()
    try:
        with urllib.request.urlopen(request, timeout=200) as resp:
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
        label = text[:6] if text else (f"思考{len(think)}" if think else "空")
        return ok, time.time() - began, label
    except Exception as exc:  # noqa: BLE001
        return False, time.time() - began, type(exc).__name__


def main() -> int:
    print(f"级联压测 {PEER}，{MINUTES} 分钟、每 {INTERVAL}s 一次", flush=True)
    deadline = time.time() + MINUTES * 60
    results: list[tuple[bool, float, str]] = []
    start_health = None
    try:
        start_health = health()
    except Exception as exc:  # noqa: BLE001
        print("起始 health 失败:", type(exc).__name__)

    index = 0
    while time.time() < deadline:
        index += 1
        ok, seconds, label = completion(openai=index % 2 == 0)
        results.append((ok, seconds, label))
        mark = "OK  " if ok else "FAIL"
        print(f"  #{index:<3} {mark} {seconds:5.1f}s {label}", flush=True)
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(INTERVAL, remaining))

    passed = sum(1 for ok, _, _ in results if ok)
    times = [s for _, s, _ in results if s]
    print(f"\n合计 {passed}/{len(results)} 成功", flush=True)
    if times:
        ordered = sorted(times)
        print(f"耗时 中位 {ordered[len(ordered) // 2]:.1f}s / "
              f"最大 {max(times):.1f}s", flush=True)
    failures = [label for ok, _, label in results if not ok]
    if failures:
        print("失败样本:", failures[:6], flush=True)
    try:
        end_health = health()
        before = (start_health or {}).get("stats") or {}
        after = end_health.get("stats") or {}
        print(f"对端 ok {before.get('ok')} -> {after.get('ok')}，"
              f"failed {before.get('failed')} -> {after.get('failed')}，"
              f"models={end_health.get('models')}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print("结束 health 失败:", type(exc).__name__)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
