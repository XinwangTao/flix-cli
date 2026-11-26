"""手机页面真机验收：一次跑完所有页面侧检查。

把第 52~55 轮分散验证的几件事合并成一个入口：
1. 页面能加载、JS 执行了、状态卡数字与后端一致；
2. 四种协议×流式组合都能发送成功；
3. 「演示工具调用」两轮跑通；
4. 复制按钮的反馈与真实结果一致（不再无条件报「已复制」）。

前置（手机浏览器里要开着自检页）：
    adb forward tcp:9333 localabstract:chrome_devtools_remote

用法：
    python tests/cdp_page_acceptance.py [ws_url]

没有 CDP 或页面没打开时整项跳过（退出码 0），这样在 CI 里也能跑。
"""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.request

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

PORT = 9333
GATEWAY_PORT = "18993"

results: list[tuple[str, bool | None, str]] = []


def record(name: str, ok: bool | None, detail: str = "") -> None:
    results.append((name, ok, detail))
    mark = "PASS" if ok is True else ("SKIP" if ok is None else "FAIL")
    print(f"[{mark}] {name:<18} {detail}", flush=True)


def find_target() -> str | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json",
                                    timeout=5) as resp:
            pages = json.loads(resp.read())
    except Exception:  # noqa: BLE001
        return None
    pages = [p for p in pages if p.get("type") == "page"
             and GATEWAY_PORT in (p.get("url") or "")]
    return pages[-1]["webSocketDebuggerUrl"] if pages else None


class Page:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.next_id = 100

    async def evaluate(self, expr: str):
        self.next_id += 1
        await self.ws.send_json({"id": self.next_id,
                                 "method": "Runtime.evaluate",
                                 "params": {"expression": expr,
                                            "returnByValue": True}})
        while True:
            message = await self.ws.receive()
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            reply = json.loads(message.data)
            if reply.get("id") != self.next_id:
                continue
            result = reply.get("result", {})
            if result.get("exceptionDetails"):
                return f"EXC: {result['exceptionDetails'].get('text')}"
            return result.get("result", {}).get("value")

    async def wait_text(self, element_id: str, predicate, limit: int = 60) -> str:
        detail = ""
        for _ in range(limit):
            await asyncio.sleep(2)
            detail = str(await self.evaluate(
                f"document.getElementById('{element_id}').textContent") or "")
            if predicate(detail):
                break
        return detail


async def run(ws_url: str) -> int:
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url, timeout=60) as ws:
            page = Page(ws)
            await ws.send_json({"id": 90, "method": "Page.reload",
                                "params": {"ignoreCache": True}})
            await asyncio.sleep(4.0)

            # 1) 页面与后端一致
            stats_text = str(await page.evaluate(
                "document.getElementById('stats').textContent") or "")
            options = int(await page.evaluate(
                "document.getElementById('model').options.length") or 0)
            ran = bool(stats_text) and "加载中" not in stats_text
            record("页面 JS 执行", ran, stats_text[:50].replace("\n", " ") if ran
                   else "状态卡没被填充")
            match = __import__("re").search(r"模型\s*(\d+)\s*个", stats_text)
            shown = int(match.group(1)) if match else -1
            record("页面与下拉一致", shown == options,
                   f"状态卡 {shown} 个 / 下拉 {options} 个")

            # 2) 四种组合
            combos = [("Anthropic 流式", False, True),
                      ("Anthropic 非流式", False, False),
                      ("OpenAI 流式", True, True),
                      ("OpenAI 非流式", True, False)]
            for label, openai, stream in combos:
                await page.evaluate("""
                  (function () {
                    document.getElementById('openai').checked = %s;
                    document.getElementById('stream').checked = %s;
                    document.getElementById('result').textContent = '';
                    document.getElementById('send').click();
                    return 1;
                  })()
                """ % ("true" if openai else "false",
                       "true" if stream else "false"))
                detail = await page.wait_text(
                    "result",
                    lambda t: "HTTP " in t or "请求失败" in t)
                ok = "HTTP 200" in detail
                record(f"发送 {label}", ok,
                       " ".join(detail.split())[:60])
                await asyncio.sleep(1)

            # 3) 工具调用两轮
            await page.evaluate("""
              (function () {
                document.getElementById('toolresult').textContent = '';
                document.getElementById('tooltest').click();
                return 1;
              })()
            """)
            detail = await page.wait_text(
                "toolresult",
                lambda t: "工具调用通过" in t or "失败" in t or "没有发起" in t,
                limit=50)
            record("工具调用两轮", "工具调用通过" in detail,
                   " ".join(detail.split())[:70])

            # 4) 复制按钮反馈是否诚实
            api = bool(await page.evaluate(
                "!!(navigator.clipboard && navigator.clipboard.writeText)"))
            fallback = str(await page.evaluate("""
              (function () {
                var ta = document.createElement('textarea');
                ta.value = 'probe'; document.body.appendChild(ta); ta.select();
                var ok = false;
                try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
                document.body.removeChild(ta);
                return String(ok);
              })()
            """)).lower() == "true"
            label_text = str(await page.evaluate("""
              (function () {
                var b = document.querySelector('[data-copy]');
                if (!b) return 'NO_BUTTON';
                b.click();
                return b.textContent;
              })()
            """))
            expect_success = api or fallback
            ok = (label_text == "已复制") if expect_success \
                else (label_text == "请长按手动复制")
            record("复制按钮诚实", ok,
                   f"真实={expect_success} 标签={label_text}")

    failed = [n for n, ok, _ in results if ok is False]
    skipped = [n for n, ok, _ in results if ok is None]
    judged = len(results) - len(skipped)
    print(f"\n{judged - len(failed)}/{judged} 通过"
          + (f"（跳过 {len(skipped)} 项）" if skipped else ""))
    return 0 if not failed else 1


def main() -> int:
    if aiohttp is None:
        print("跳过：未安装 aiohttp")
        return 0
    ws_url = sys.argv[1] if len(sys.argv) > 1 else find_target()
    if not ws_url:
        record("页面验收", None,
               "跳过（没有 CDP 或浏览器里没开着自检页；先 adb forward "
               "tcp:9333 localabstract:chrome_devtools_remote）")
        return 0
    print("target:", ws_url[:70])
    try:
        return asyncio.run(run(ws_url))
    except Exception as exc:  # noqa: BLE001
        record("页面验收", False, f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
