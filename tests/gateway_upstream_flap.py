"""上游抖动自愈：cc-switch 短暂不可用后又回来，网关应自动恢复。

真实现场：用户切 VPN、cc-switch 崩溃被拉起、上游 provider 短暂 5xx。
用户不应该因此重启网关，也不应该等 10 分钟熔断过期。

做法：起一个假上游（可随时停/起），网关指向它，观察：
  1. 正常时能补全；
  2. 上游停掉 -> 报错（快，不挂死）；
  3. 上游恢复 -> **不用重启网关**，几秒内自动可用。
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

REPLY = {
    "id": "msg_flap", "type": "message", "role": "assistant",
    "model": "m1", "content": [{"type": "text", "text": "通了"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 3, "output_tokens": 2},
}
CATALOG = {"data": [{"id": "m1", "display_name": "M1"}]}


def _cfg(tmp: str, upstream: str) -> dict:
    return {
        "enabled": True, "port": 0, "token": "rd-test-token",
        "upstream": upstream, "max_body_mb": 32,
        "max_concurrent": 8, "upstream_concurrency": 4,
        "queue_wait_s": 5, "upstream_timeout_s": 20,
        "allow_remote_upstream": False, "upstream_token": "",
        "hide_dead_relays": True,
    }


async def _upstream_app() -> web.Application:
    app = web.Application()

    async def models(_request):
        return web.json_response(CATALOG)

    async def messages(_request):
        return web.json_response(REPLY)

    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/messages", messages)
    return app


async def run() -> int:
    results: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<26} {detail}", flush=True)

    with tempfile.TemporaryDirectory() as tmp:
        ups = TestServer(await _upstream_app())
        await ups.start_server()
        up_url = str(ups.make_url("")).rstrip("/")
        # 必须在关闭前记下端口：close() 之后 ups.port 会变成 0/None，
        # 拿它去重建上游会绑到随机端口，网关自然永远连不上（这是测试自身的坑，
        # 曾让我误以为「上游恢复后要 178 秒才好」）。
        port = ups.port

        cfg = _cfg(tmp, up_url)
        client = TestClient(TestServer(gw.make_model_app(None, cfg)))
        await client.start_server()
        headers = {"x-api-key": cfg["token"]}

        async def complete() -> tuple[int, str]:
            resp = await client.post(
                "/v1/messages", headers=headers,
                json={"model": "m1", "max_tokens": 16,
                      "messages": [{"role": "user", "content": "hi"}]})
            text = await resp.text()
            return resp.status, text

        try:
            status, text = await complete()
            record("上游正常时可用", status == 200, f"HTTP {status}")

            # 上游下线
            await ups.close()
            began = time.time()
            status, text = await complete()
            elapsed = time.time() - began
            record("上游下线时报错但不挂死", status >= 500 and elapsed < 30,
                   f"HTTP {status}，{elapsed:.1f}s")

            # 同一端口重新起来（模拟 cc-switch 被拉起）
            again = TestServer(await _upstream_app(), port=port)
            await again.start_server()
            try:
                recovered = False
                began = time.time()
                for _ in range(30):
                    status, text = await complete()
                    if status == 200:
                        recovered = True
                        break
                    await asyncio.sleep(1)
                took = time.time() - began
                record("上游恢复后自动可用", recovered,
                       f"恢复用时 {took:.1f}s（未重启网关）")
            finally:
                await again.close()
        finally:
            await client.close()

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} 通过")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
