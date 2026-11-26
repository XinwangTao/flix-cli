"""上游返回异常数据时，网关会不会崩、会不会把垃圾转给客户端。

三种：
  1. 超大响应（8 MB 的非流式回包）—— 网关应该照常转发，不该 OOM 或截断；
  2. 畸形 JSON（非流式）—— 上游坏了，客户端该收到一个可理解的错误；
  3. 空响应体 —— 同上。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from flixcli import model_gateway as gw  # noqa: E402

BIG_TEXT = "x" * (8 * 1024 * 1024)


def make_upstream(kind: str) -> web.Application:
    app = web.Application()

    async def models(_r):
        return web.json_response({"data": [{"id": "m1", "display_name": "M1"}]})

    async def messages(_r):
        if kind == "big":
            return web.json_response({
                "id": "m", "type": "message", "role": "assistant", "model": "m1",
                "content": [{"type": "text", "text": BIG_TEXT}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1}})
        if kind == "garbage":
            return web.Response(body=b"<html>502 upstream buffering</html>",
                                content_type="application/json")
        if kind == "empty":
            return web.Response(body=b"", content_type="application/json")
        raise AssertionError(kind)

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
    began = time.time()
    try:
        r = await client.post("/v1/messages", headers={"x-api-key": "rd-t"},
                              json={"model": "m1", "max_tokens": 8,
                                    "messages": [{"role": "user", "content": "hi"}]})
        took = time.time() - began
        body = await r.read()
        print(f"  [{kind:<8}] {took:5.1f}s  HTTP {r.status}  "
              f"{len(body):>9} 字节  {body[:60]!r}", flush=True)
    except Exception as exc:  # noqa: BLE001
        took = time.time() - began
        print(f"  [{kind:<8}] {took:5.1f}s  EXC {type(exc).__name__}: {exc}",
              flush=True)
    finally:
        await client.close()
        await ups.close()


async def main() -> int:
    with tempfile.TemporaryDirectory():
        for kind in ("big", "garbage", "empty"):
            await probe(kind)
    print()
    print("判据：big 应完整转发（8 MB）；garbage/empty 应回 502 而不是 200 + 垃圾。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
