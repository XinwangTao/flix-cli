"""目录端点（/v1/models）的两种异常：上游回垃圾、回空列表。

真实场景：cc-switch 还在但背后没配任何账号 -> 空目录；
中间代理塞错误页 -> 非 JSON。
客户端（手机 App 的下拉框）拿到这两种结果时体验差别很大，值得确认网关怎么处理。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from flixcli import model_gateway as gw  # noqa: E402


def make_upstream(kind: str) -> web.Application:
    app = web.Application()

    async def models(_r):
        if kind == "garbage":
            return web.Response(body=b"<html>502</html>",
                                content_type="application/json")
        if kind == "empty-data":
            return web.json_response({"data": []})
        if kind == "empty-object":
            return web.json_response({})
        return web.json_response({"data": [{"id": "m1", "display_name": "M1"}]})

    async def messages(_r):
        return web.json_response({"id": "m", "type": "message",
                                  "role": "assistant", "model": "m1",
                                  "content": [{"type": "text", "text": "ok"}],
                                  "stop_reason": "end_turn",
                                  "usage": {"input_tokens": 1, "output_tokens": 1}})

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
        r = await client.get("/v1/models", headers={"x-api-key": "rd-t"})
        body = await r.read()
        text = body.decode("utf-8", "replace")
        print(f"  [{kind:<13}] HTTP {r.status}  {len(body):>5} 字节  "
              f"{text[:80]!r}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  [{kind:<13}] EXC {type(exc).__name__}: {exc}", flush=True)
    finally:
        await client.close()
        await ups.close()


async def main() -> int:
    with tempfile.TemporaryDirectory():
        for kind in ("normal", "garbage", "empty-data", "empty-object"):
            await probe(kind)
    print()
    print("判据：garbage 不该原样透传；empty-data/empty-object 应是合法 JSON（客户端")
    print("      能解析成空列表），而不是 HTML 或崩掉。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
