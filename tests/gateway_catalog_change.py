"""上游目录变化：用户在 cc-switch 里新增模型，网关多久能看到？

真实现场：用户在 cc-switch 加了个模型，手机上却看不到/报「未知模型」，
会以为「共享没生效」。这里量两件事：
  1. 新增模型后，/v1/models 多久反映出来；
  2. 在反映之前直接请求那个新模型，会得到什么（400 未知模型？还是自动重取目录？）。
"""
from __future__ import annotations

import asyncio
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

state: dict = {"ids": ["m1"]}


def app() -> web.Application:
    a = web.Application()

    async def models(_r):
        return web.json_response(
            {"data": [{"id": i, "display_name": i} for i in state["ids"]]})

    async def messages(_r):
        return web.json_response(REPLY)

    a.router.add_get("/v1/models", models)
    a.router.add_post("/v1/messages", messages)
    return a


async def main() -> int:
    results: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<30} {detail}", flush=True)

    with tempfile.TemporaryDirectory() as tmp:
        ups = TestServer(app())
        await ups.start_server()
        cfg = {"enabled": True, "port": 0, "token": "rd-t",
               "upstream": str(ups.make_url("")).rstrip("/"),
               "max_body_mb": 32, "max_concurrent": 8,
               "upstream_concurrency": 4, "queue_wait_s": 5,
               "upstream_timeout_s": 20, "allow_remote_upstream": False,
               "upstream_token": "", "hide_dead_relays": True}
        client = TestClient(TestServer(gw.make_model_app(None, cfg)))
        await client.start_server()
        headers = {"x-api-key": "rd-t"}

        async def listed() -> set[str]:
            r = await client.get("/v1/models", headers=headers)
            body = await r.json()
            return {row["id"] for row in body.get("data", [])}

        try:
            first = await listed()
            record("初始目录", "m1" in first, f"{sorted(first)}")

            # 上游新增一个模型
            state["ids"] = ["m1", "m2-new"]
            # 直接请求新模型（此时网关缓存里还没有它）
            r = await client.post("/v1/messages", headers=headers,
                                  json={"model": "m2-new", "max_tokens": 8,
                                        "messages": [{"role": "user",
                                                      "content": "hi"}]})
            text = await r.text()
            passed_now = r.status == 200
            record("新模型立刻可用", passed_now,
                   f"HTTP {r.status} {text[:200]}")

            # 等目录刷新窗口
            began = time.time()
            appeared = False
            for _ in range(60):
                if "m2-new" in await listed():
                    appeared = True
                    break
                await asyncio.sleep(2)
            took = time.time() - began
            record("目录刷新看到新模型", appeared, f"用时 {took:.0f}s")
        finally:
            await client.close()

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} 通过")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
