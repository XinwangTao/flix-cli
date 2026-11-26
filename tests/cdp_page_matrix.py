"""在手机页面上真跑四种组合：两种协议 × 流式/非流式。

页面有两个勾选框（用 OpenAI 协议、流式显示）。此前只在默认组合
（Anthropic + 流式）下点过一次「发送测试」，另外三条路径没验过。

前置（手机浏览器里要开着自检页）：
    adb forward tcp:9333 localabstract:chrome_devtools_remote

用法：python tests/cdp_page_matrix.py [ws_url]
"""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.request

import aiohttp

PORT = 9333


def pick_target() -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=10) as resp:
        pages = json.loads(resp.read())
    pages = [p for p in pages if p.get("type") == "page"
             and "18993" in (p.get("url") or "")]
    if not pages:
        raise SystemExit("没有找到自检页目标")
    return pages[-1]["webSocketDebuggerUrl"]


async def evaluate(ws, expr: str, msg_id: int) -> dict:
    await ws.send_json({"id": msg_id, "method": "Runtime.evaluate",
                        "params": {"expression": expr, "returnByValue": True}})
    while True:
        message = await ws.receive()
        if message.type != aiohttp.WSMsgType.TEXT:
            continue
        reply = json.loads(message.data)
        if reply.get("id") == msg_id:
            return reply


def value_of(reply: dict):
    result = reply.get("result", {})
    if result.get("exceptionDetails"):
        return f"EXC: {result['exceptionDetails'].get('text')}"
    return result.get("result", {}).get("value")


COMBOS = [
    ("Anthropic 流式", False, True),
    ("Anthropic 非流式", False, False),
    ("OpenAI 流式", True, True),
    ("OpenAI 非流式", True, False),
]


async def run(ws_url: str) -> int:
    failures = 0
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url, timeout=60) as ws:
            await ws.send_json({"id": 90, "method": "Page.reload",
                                "params": {"ignoreCache": True}})
            await asyncio.sleep(4.0)

            msg_id = 100
            for label, openai, stream in COMBOS:
                msg_id += 1
                await evaluate(ws, """
                  (function () {
                    var o = document.getElementById('openai');
                    var s = document.getElementById('stream');
                    o.checked = %s; s.checked = %s;
                    document.getElementById('result').textContent = '';
                    document.getElementById('send').click();
                    return 'clicked';
                  })()
                """ % ("true" if openai else "false",
                       "true" if stream else "false"), msg_id)

                detail = ""
                ok = False
                for _ in range(60):
                    await asyncio.sleep(2)
                    msg_id += 1
                    detail = str(value_of(await evaluate(
                        ws, "document.getElementById('result').textContent",
                        msg_id)) or "")
                    if "HTTP 200" in detail or "HTTP 4" in detail or "HTTP 5" in detail:
                        ok = "HTTP 200" in detail
                        if stream:
                            # 流式要等到出现「（流式）」或正文
                            ok = ok and ("流式）" in detail or len(detail) > 40)
                        break
                    if "请求失败" in detail:
                        break
                flat = " ".join(detail.split())[:80]
                print(f"[{'PASS' if ok else 'FAIL'}] {label:<16} {flat}")
                if not ok:
                    failures += 1
                await asyncio.sleep(1)
    print(f"\n{len(COMBOS) - failures}/{len(COMBOS)} 组合通过")
    return 0 if failures == 0 else 1


def main() -> int:
    ws_url = sys.argv[1] if len(sys.argv) > 1 else pick_target()
    return asyncio.run(run(ws_url))


if __name__ == "__main__":
    raise SystemExit(main())
