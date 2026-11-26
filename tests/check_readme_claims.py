"""抽查 README 里对 OpenAI 兼容层的具体声明，是否真的实现了。

README 写得很细，比如「system/developer -> 顶层 system」「max_completion_tokens」
「stream_options.include_usage」「thinking 放在 reasoning_content」。这些如果没实现，
用户照着接会踩坑。这里逐条打真网关验证。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BASE = os.environ.get("RD_GATEWAY_URL", "http://127.0.0.1:18993")
TOKEN = os.environ.get("RD_GATEWAY_TOKEN", "")
MODEL = os.environ.get("RD_MODEL", "glm-5.3-flash")
if not TOKEN:
    raise SystemExit("请设置环境变量 RD_GATEWAY_TOKEN（桌面端「模型共享」面板可复制）")

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name:<30} {detail}", flush=True)


def post(payload: dict) -> tuple[int, dict | str]:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        BASE + "/v1/chat/completions", data=body,
        headers={"authorization": f"Bearer {TOKEN}",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=120) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def main() -> int:
    # 1) developer 角色应被当成 system
    status, data = post({
        "model": MODEL, "max_tokens": 24,
        "messages": [
            {"role": "developer", "content": "You always answer OK."},
            {"role": "user", "content": "say OK"},
        ]})
    ok = status == 200 and isinstance(data, dict) and data.get("choices")
    record("developer 角色可用", ok, f"HTTP {status}")

    # 2) max_completion_tokens 应被接受
    status, data = post({
        "model": MODEL, "max_completion_tokens": 24,
        "messages": [{"role": "user", "content": "hi"}]})
    record("max_completion_tokens 可用", status == 200, f"HTTP {status}")

    # 3) 响应形状：object / choices[].message / usage
    if isinstance(data, dict):
        shape_ok = (data.get("object", "").startswith("chat.completion")
                    and data.get("choices")
                    and "message" in data["choices"][0]
                    and "usage" in data)
        record("响应是 OpenAI 形状", bool(shape_ok),
               f"object={data.get('object')}")
        msg = (data.get("choices") or [{}])[0].get("message", {})
        has_text = bool(msg.get("content")) or bool(msg.get("reasoning_content"))
        record("正文或 thinking 至少有一个", has_text,
               f"content={bool(msg.get('content'))} "
               f"reasoning={bool(msg.get('reasoning_content'))}")

    # 4) stream_options.include_usage -> 流里应有 usage 事件
    body = json.dumps({
        "model": MODEL, "max_tokens": 24, "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    request = urllib.request.Request(
        BASE + "/v1/chat/completions", data=body,
        headers={"authorization": f"Bearer {TOKEN}",
                 "content-type": "application/json"})
    saw_usage = False
    saw_done = False
    try:
        with urllib.request.urlopen(request, timeout=120) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    saw_done = True
                    continue
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                if chunk.get("usage"):
                    saw_usage = True
    except Exception as exc:  # noqa: BLE001
        record("include_usage 流式", False, f"{type(exc).__name__}: {exc}")
    else:
        record("include_usage 有 usage 事件", saw_usage, f"usage={saw_usage}")
        record("流以 [DONE] 结束", saw_done, f"done={saw_done}")

    # 5) image_url part 应被接受（不发真图，用一个 1x1 data URL）
    tiny = ("data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
            "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    status, data = post({
        "model": MODEL, "max_tokens": 16,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "what color?"},
            {"type": "image_url", "image_url": {"url": tiny}},
        ]}]})
    record("image_url part 可用", status == 200, f"HTTP {status}")

    # 6) tool_choice=none 不应下发工具（只验证不报错）
    status, data = post({
        "model": MODEL, "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {
            "name": "noop", "description": "does nothing",
            "parameters": {"type": "object", "properties": {}}}}],
        "tool_choice": "none"})
    record("tool_choice=none 可用", status == 200, f"HTTP {status}")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} 通过")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
