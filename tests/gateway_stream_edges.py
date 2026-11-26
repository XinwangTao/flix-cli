"""流式响应的另一种「假成功」：上游在流里发错误事件，或者流是空的。

三种：
  1. 流里出现 Anthropic 的 error 事件（`{"type":"error"}`）—— 客户端该看到失败；
  2. 流里只有 [DONE]，没有任何内容 delta —— 空流；
  3. 正常流（对照）。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from flixcli import model_gateway as gw  # noqa: E402


def sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()


def make_upstream(kind: str) -> web.Application:
    app = web.Application()

    async def models(_r):
        return web.json_response({"data": [{"id": "m1", "display_name": "M1"}]})

    async def messages(_r):
        resp = web.StreamResponse(
            status=200, headers={"content-type": "text/event-stream"})
        await resp.prepare(_r)
        if kind == "error-in-stream":
            await resp.write(sse("message_start", {
                "type": "message_start",
                "message": {"id": "m", "type": "message", "role": "assistant",
                            "model": "m1", "content": [],
                            "usage": {"input_tokens": 1, "output_tokens": 0}}}))
            await resp.write(sse("error", {
                "type": "error",
                "error": {"type": "overloaded_error",
                          "message": "upstream gave up mid-stream"}}))
        elif kind == "empty":
            await resp.write(sse("message_start", {
                "type": "message_start",
                "message": {"id": "m", "type": "message", "role": "assistant",
                            "model": "m1", "content": [],
                            "usage": {"input_tokens": 1, "output_tokens": 0}}}))
            await resp.write(sse("message_stop", {"type": "message_stop"}))
        else:
            await resp.write(sse("message_start", {
                "type": "message_start",
                "message": {"id": "m", "type": "message", "role": "assistant",
                            "model": "m1", "content": [],
                            "usage": {"input_tokens": 1, "output_tokens": 0}}}))
            await resp.write(sse("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": "ok"}}))
            await resp.write(sse("message_stop", {"type": "message_stop"}))
        await resp.write_eof()
        return resp

    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/messages", messages)
    return app


async def probe(kind: str) -> None:
    ups = TestServer(make_upstream(kind))
    await ups.start_server()
    cfg = {"enabled": True, "port": 0, "token": "rd-t",
           "upstream": str(ups.make_url("")).rstrip("/"),
           "max_body_mb": 32, "max_concurrent": 8,
           "upstream_concurrency": 4, "queue_wait_s": 5,
           "upstream_timeout_s": 60, "allow_remote_upstream": False,
           "upstream_token": "", "hide_dead_relays": True}
    client = TestClient(TestServer(gw.make_model_app(None, cfg)))
    await client.start_server()
    try:
        r = await client.post("/v1/messages", headers={"x-api-key": "rd-t"},
                              json={"model": "m1", "max_tokens": 8,
                                    "stream": True,
                                    "messages": [{"role": "user", "content": "hi"}]})
        raw = await r.read()
        text = raw.decode("utf-8", "replace")
        has_error = '"type": "error"' in text or '"type":"error"' in text
        has_text = '"text_delta"' in text or '"text":' in text
        print(f"  [{kind:<16}] HTTP {r.status}  {len(raw):>4} 字节  "
              f"error事件={has_error}  有正文={has_text}", flush=True)
        print(f"     前 90 字: {text[:90]!r}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  [{kind:<16}] EXC {type(exc).__name__}: {exc}", flush=True)
    finally:
        await client.close()
        await ups.close()


async def main() -> int:
    with tempfile.TemporaryDirectory():
        for kind in ("normal", "error-in-stream", "empty"):
            await probe(kind)
    print()
    print("判据：normal 应有正文；error-in-stream 应把 error 事件透给客户端；")
    print("      empty 是上游的行为，网关只能原样传（但客户端会看到 0 内容）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
