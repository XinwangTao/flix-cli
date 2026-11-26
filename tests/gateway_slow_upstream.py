"""上游很慢时网关怎么办：慢响应、慢首包、彻底不回。

真实现场：cc-switch 背后的 provider 有免费额度，实测 1.0–27 秒波动，偶尔更慢。
用户不该因此挂死或收到莫名其妙的错误。

三种情况分开测：
  1. 上游 8 秒才回（慢但正常）-> 应该成功，不该提前超时；
  2. 上游 45 秒才回（很慢）-> 视 upstream_timeout_s 决定；
  3. 上游永不回 -> 应该按预算超时并回结构化错误，不是挂死。
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

REPLY = {"id": "m", "type": "message", "role": "assistant", "model": "m1",
         "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
         "usage": {"input_tokens": 1, "output_tokens": 1}}


def make_upstream(delay: float | None) -> web.Application:
    app = web.Application()

    async def models(_r):
        return web.json_response({"data": [{"id": "m1", "display_name": "M1"}]})

    async def messages(_r):
        if delay is None:
            await asyncio.sleep(600)      # 永不回
        else:
            await asyncio.sleep(delay)
        return web.json_response(REPLY)

    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/messages", messages)
    return app


async def probe(delay: float | None, timeout_s: int, label: str) -> None:
    ups = TestServer(make_upstream(delay))
    await ups.start_server()
    cfg = {"enabled": True, "port": 0, "token": "rd-t",
           "upstream": str(ups.make_url("")).rstrip("/"),
           "max_body_mb": 32, "max_concurrent": 8,
           "upstream_concurrency": 4, "queue_wait_s": 5,
           "upstream_timeout_s": timeout_s, "allow_remote_upstream": False,
           "upstream_token": "", "hide_dead_relays": True}
    client = TestClient(TestServer(gw.make_model_app(None, cfg)))
    await client.start_server()
    began = time.time()
    try:
        r = await client.post("/v1/messages",
                              headers={"x-api-key": "rd-t"},
                              json={"model": "m1", "max_tokens": 8,
                                    "messages": [{"role": "user",
                                                  "content": "hi"}]})
        took = time.time() - began
        body = await r.text()
        print(f"  [{label}] {took:5.1f}s  HTTP {r.status}  {body[:70]}",
              flush=True)
    except Exception as exc:  # noqa: BLE001
        took = time.time() - began
        print(f"  [{label}] {took:5.1f}s  EXC {type(exc).__name__}: {exc}",
              flush=True)
    finally:
        await client.close()
        await ups.close()


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        print("1) 上游 8 秒才回（慢但正常），timeout=30")
        await probe(8.0, 30, "8s")
        print("\n2) 上游 45 秒才回，timeout=12（预算不足）")
        await probe(45.0, 12, "45s/12")
        print("\n3) 上游永不回，timeout=8")
        await probe(None, 8, "never/8")
    print()
    print("判据：1 应 200；2、3 应在预算附近返回结构化错误，不能挂死。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
