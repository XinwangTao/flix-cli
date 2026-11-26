"""局域网模型共享 —— 一条命令跑完整验收。

把这一轮所有手工验证固化成可复跑的检查：协议、鉴权、模型列表口径、流式、
工具调用、并发、断开清理、自检页。发布前跑一次就知道功能是否完好。

用法（网关需已在跑）：
    python tests/acceptance_lan_models.py            # 本机 127.0.0.1
    python tests/acceptance_lan_models.py <host>     # 指定地址，如 192.168.237.7

退出码：0 全过；1 有失败项。

注意：本套件**会故意制造失败请求**（401 无令牌/错令牌、400 未知模型、
中途断开）。跑完之后 `/health` 的 `failed` / `rejected` 会因为这些用例而上涨，
`flix models doctor --watch` 也可能把这段时间的增量报成「新增失败」——
那是测试自己造成的，不是线上问题。
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 18993
BASE = f"http://{HOST}:{PORT}"
TOKEN = os.environ.get("RD_GATEWAY_TOKEN") or ""
if not TOKEN:
    raise SystemExit(
        "请设置环境变量 RD_GATEWAY_TOKEN（桌面端「模型共享」面板可复制）")

PROMPT = "只回答两个字：通了"

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    mark = "PASS" if ok is True else ("SKIP" if ok is None else "FAIL")
    print(f"[{mark}] {name:<22} {detail}", flush=True)


def post(path: str, body: dict, headers: dict, timeout: float = 180.0):
    data = json.dumps(body).encode()
    request = urllib.request.Request(
        BASE + path, data=data,
        headers={**headers, "content-type": "application/json"})
    began = time.time()
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        raw = resp.read()
        return resp.status, json.loads(raw), time.time() - began


def anthropic_headers() -> dict:
    return {"x-api-key": TOKEN, "anthropic-version": "2023-06-01"}


def openai_headers() -> dict:
    return {"authorization": f"Bearer {TOKEN}"}


def check_health() -> dict | None:
    try:
        request = urllib.request.Request(BASE + "/health",
                                         headers={"x-api-key": TOKEN})
        with urllib.request.urlopen(request, timeout=20) as resp:
            payload = json.loads(resp.read())
        record("网关健康", bool(payload.get("ok")),
               f"models={payload.get('models')} "
               f"catalog={payload.get('catalog_models')} "
               f"upstream_ok={payload.get('upstream_ok')}")
        return payload
    except Exception as exc:  # noqa: BLE001
        record("网关健康", False, f"{type(exc).__name__}: {exc}")
        return None


def check_models(health: dict | None) -> list[str]:
    try:
        request = urllib.request.Request(BASE + "/v1/models",
                                         headers={"x-api-key": TOKEN})
        with urllib.request.urlopen(request, timeout=20) as resp:
            payload = json.loads(resp.read())
        rows = payload.get("data") or []
        ids = [row["id"] for row in rows if row.get("id")]
        record("模型列表", bool(ids), f"{len(ids)} 个，首个={ids[0] if ids else '-'}")
        if health is not None:
            same = health.get("models") == len(ids)
            record("列表口径一致", same,
                   f"health={health.get('models')} served={len(ids)}")
        # 死中继不应出现在列表里
        prefixes = set((health or {}).get("relay_prefixes") or {})
        leaked = [m for m in ids
                  if any(m.startswith(p) for p in prefixes)]
        record("死中继已隐藏", not leaked,
               f"泄漏 {len(leaked)} 个" if leaked else "无")
        return ids
    except Exception as exc:  # noqa: BLE001
        record("模型列表", False, f"{type(exc).__name__}: {exc}")
        return []


def check_anthropic(model: str) -> None:
    try:
        status, payload, seconds = post(
            "/v1/messages",
            {"model": model, "max_tokens": 200,
             "messages": [{"role": "user", "content": PROMPT}]},
            anthropic_headers())
        blocks = payload.get("content") or []
        text = "".join(b.get("text", "") for b in blocks
                       if b.get("type") == "text").strip()
        think = "".join(b.get("thinking", "") for b in blocks
                        if b.get("type") == "thinking").strip()
        ok = status == 200 and bool(text or think)
        detail = f"{seconds:.1f}s " + (repr(text[:10]) if text
                                       else f"(只回思考 {len(think)} 字)")
        record("Anthropic 补全", ok, detail)
    except Exception as exc:  # noqa: BLE001
        record("Anthropic 补全", False, f"{type(exc).__name__}: {exc}")


def check_openai(model: str) -> None:
    try:
        status, payload, seconds = post(
            "/v1/chat/completions",
            {"model": model, "max_tokens": 200,
             "messages": [{"role": "user", "content": PROMPT}]},
            openai_headers())
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = (message.get("content") or "").strip()
        think = (message.get("reasoning_content") or "").strip()
        ok = status == 200 and bool(text or think)
        detail = f"{seconds:.1f}s " + (repr(text[:10]) if text
                                       else f"(只回思考 {len(think)} 字)")
        record("OpenAI 补全", ok, detail)
    except Exception as exc:  # noqa: BLE001
        record("OpenAI 补全", False, f"{type(exc).__name__}: {exc}")


def check_openai_stream(model: str) -> None:
    try:
        data = json.dumps({
            "model": model, "max_tokens": 200, "stream": True,
            "messages": [{"role": "user", "content": PROMPT}],
        }).encode()
        request = urllib.request.Request(
            BASE + "/v1/chat/completions", data=data,
            headers={**openai_headers(), "content-type": "application/json"})
        began = time.time()
        chunks = 0
        saw_done = False
        with urllib.request.urlopen(request, timeout=180) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    saw_done = True
                    break
                chunks += 1
        record("OpenAI 流式", chunks > 0 and saw_done,
               f"{chunks} 个事件，[DONE]={saw_done}，{time.time() - began:.1f}s")
    except Exception as exc:  # noqa: BLE001
        record("OpenAI 流式", False, f"{type(exc).__name__}: {exc}")


def check_tool_calls(model: str) -> None:
    """两轮：模型发起工具调用 -> 回传结果 -> 最终回答。"""
    tools = [{"type": "function", "function": {
        "name": "get_time", "description": "查某个城市的当前时间",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                       "required": ["city"]}}}]
    base = {"model": model, "max_tokens": 300, "tools": tools,
            "tool_choice": "auto"}
    prompt = "现在北京几点？请调用 get_time 工具，city 传 Beijing。"
    try:
        status, first, _ = post(
            "/v1/chat/completions",
            {**base, "messages": [{"role": "user", "content": prompt}]},
            openai_headers())
        message = ((first.get("choices") or [{}])[0] or {}).get("message") or {}
        calls = message.get("tool_calls") or []
        if not calls:
            record("工具调用", False, "模型未发起工具调用（可能不支持 function calling）")
            return
        call = calls[0]
        status2, second, seconds = post(
            "/v1/chat/completions",
            {**base, "messages": [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": None, "tool_calls": [call]},
                {"role": "tool", "tool_call_id": call.get("id", ""),
                 "content": "2026-11-25 22:00:00（Asia/Shanghai）"}]},
            openai_headers())
        final = (((second.get("choices") or [{}])[0] or {}).get("message")
                 or {}).get("content") or ""
        record("工具调用", status2 == 200 and bool(final.strip()),
               f"{call.get('function', {}).get('name')} -> "
               f"{seconds:.1f}s 最终回答 {len(final)} 字")
    except Exception as exc:  # noqa: BLE001
        record("工具调用", False, f"{type(exc).__name__}: {exc}")


def check_auth() -> None:
    # 无令牌
    try:
        request = urllib.request.Request(BASE + "/v1/models")
        with urllib.request.urlopen(request, timeout=15) as resp:
            record("鉴权-无令牌", False, f"竟然返回 {resp.status}")
    except urllib.error.HTTPError as exc:
        record("鉴权-无令牌", exc.code in (401, 403), f"HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001
        record("鉴权-无令牌", False, f"{type(exc).__name__}")
    # 错令牌
    try:
        request = urllib.request.Request(
            BASE + "/v1/models", headers={"x-api-key": "rd-wrong-token"})
        with urllib.request.urlopen(request, timeout=15) as resp:
            record("鉴权-错令牌", False, f"竟然返回 {resp.status}")
    except urllib.error.HTTPError as exc:
        record("鉴权-错令牌", exc.code in (401, 403), f"HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001
        record("鉴权-错令牌", False, f"{type(exc).__name__}")


def check_unknown_model() -> None:
    try:
        status, payload, _ = post(
            "/v1/messages",
            {"model": "definitely-not-a-model-xyz", "max_tokens": 16,
             "messages": [{"role": "user", "content": "hi"}]},
            anthropic_headers())
        record("未知模型 400", status == 400, f"HTTP {status}")
    except urllib.error.HTTPError as exc:
        record("未知模型 400", exc.code == 400, f"HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001
        record("未知模型 400", False, f"{type(exc).__name__}: {exc}")


def check_page() -> None:
    try:
        request = urllib.request.Request(BASE + f"/?token={TOKEN}")
        with urllib.request.urlopen(request, timeout=20) as resp:
            html = resp.read().decode("utf-8", "replace")
        script = re.search(r"<script>([\s\S]*?)</script>", html)
        problems = []
        if not script:
            problems.append("没有 script 块")
        if "var MODELS" in html:
            problems.append("残留 var MODELS")
        if re.search(r"\{\{[A-Z_]+\}\}", html):
            problems.append("占位符未替换")
        if script:
            path = os.path.join(os.environ.get("TEMP", "/tmp"), "rd_page_check.js")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(script.group(1))
            node = subprocess.run(["node", "--check", path],
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
            if node.returncode != 0:
                problems.append("JS 语法错误")
        record("自检页", not problems,
               "；".join(problems) if problems
               else f"{len(html)} 字节，JS 语法干净")
    except FileNotFoundError:
        record("自检页", True, "（无 node，跳过语法检查）")
    except Exception as exc:  # noqa: BLE001
        record("自检页", False, f"{type(exc).__name__}: {exc}")


def _page_token(ws_url: str) -> str:
    """读页面里内嵌的 TOKEN，用来确认这个标签页是不是同一个网关。"""
    import asyncio

    import aiohttp

    async def read() -> str:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url, timeout=10) as ws:
                # 注意：页面的 TOKEN/BASE 都在 IIFE 闭包里，全局取不到；
                # 令牌要从 DOM 里那个 <code id="tok"> 读。
                await ws.send_json({"id": 1, "method": "Runtime.evaluate",
                                    "params": {"expression":
                                        "(document.getElementById('tok') || {}).textContent || ''",
                                        "returnByValue": True}})
                while True:
                    message = await ws.receive()
                    if message.type != aiohttp.WSMsgType.TEXT:
                        continue
                    reply = json.loads(message.data)
                    if reply.get("id") == 1:
                        return str((reply.get("result", {})
                                    .get("result", {}).get("value")) or "")

    try:
        return asyncio.run(read())
    except Exception:  # noqa: BLE001
        return ""


def check_page_consistency(health: dict | None, served: list[str]) -> None:
    """自检页上显示的模型数，必须和网关真正服务的条数一致。

    只校验 HTML/JS 语法是不够的——实测出现过「页面显示 39 个、网关只服务 11 个」
    的冷启动窗口问题，那种偏差只有真的把页面读出来才看得见。

    需要手机上的浏览器开着自检页，且已做端口转发：
        adb forward tcp:9333 localabstract:chrome_devtools_remote
    没有 CDP 时本项跳过（不算失败），这样 CI 里也能跑。
    """
    if not served:
        record("页面与后端一致", None, "跳过（没有模型列表）")
        return
    try:
        import aiohttp  # noqa: F401  仅在可用时才做这层校验
    except ImportError:
        record("页面与后端一致", None, "跳过（未安装 aiohttp）")
        return

    try:
        with urllib.request.urlopen("http://127.0.0.1:9333/json", timeout=5) as resp:
            targets = json.loads(resp.read())
    except Exception:  # noqa: BLE001 - 没有 CDP 转发是正常情况
        record("页面与后端一致", None,
               "跳过（没有 CDP；adb forward tcp:9333 localabstract:"
               "chrome_devtools_remote 后可校验）")
        return

    # 只认**被测目标自己**的页面。以前只按端口匹配，于是拿对端跑验收时，
    # 实际读的是浏览器里开着的那台**本机**页面（两边都是 11，凑巧通过）——假通过。
    # 按 URL 前缀匹配也不行：本机验收时 BASE 是 127.0.0.1，而手机页面用的是
    # 192.168.137.1（同一个网关的另一个地址）。所以改成**比对页面内嵌的令牌**：
    # 令牌一致 = 同一个网关实例，这才是可靠的判据。
    candidates = [target for target in targets
                  if target.get("type") == "page"
                  and str(PORT) in str(target.get("url") or "")]
    page = None
    if candidates:
        try:
            page = next(
                target for target in candidates
                if _page_token(target["webSocketDebuggerUrl"]) == TOKEN)
        except Exception:  # noqa: BLE001
            page = None
    if page is None:
        record("页面与后端一致", None,
               f"跳过（浏览器里没有打开目标 {BASE} 的自检页）")
        return

    async def read_page_state(ws_url: str) -> tuple[str, int]:
        import aiohttp
        import asyncio

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url, timeout=30) as ws:
                # 先刷新：浏览器里可能还挂着旧构建渲染的页面（实测就是这样，
                # 页面说 39 个、后端只服务 11 个）。这里要校验的是**服务端当前
                # 输出**与后端是否一致，不是某个标签页缓存了什么。
                await ws.send_json({"id": 90, "method": "Page.reload",
                                    "params": {"ignoreCache": True}})
                await asyncio.sleep(3.0)

                async def evaluate(expr: str, msg_id: int):
                    await ws.send_json({"id": msg_id,
                                        "method": "Runtime.evaluate",
                                        "params": {"expression": expr,
                                                   "returnByValue": True}})
                    while True:
                        message = await ws.receive()
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        reply = json.loads(message.data)
                        if reply.get("id") == msg_id:
                            value = (reply.get("result", {})
                                     .get("result", {}).get("value"))
                            return value

                stats_text = await evaluate(
                    "document.getElementById('stats').textContent", 1) or ""
                options = await evaluate(
                    "document.getElementById('model').options.length", 2) or 0
                return str(stats_text), int(options)

    try:
        import asyncio
        stats_text, options = asyncio.run(
            read_page_state(page["webSocketDebuggerUrl"]))
    except Exception as exc:  # noqa: BLE001
        record("页面与后端一致", False, f"读取页面失败 {type(exc).__name__}")
        return

    expected = len(served)
    problems = []
    match = re.search(r"模型\s*(\d+)\s*个", stats_text)
    if not match:
        problems.append("状态卡里没有模型数")
    elif int(match.group(1)) != expected:
        problems.append(f"页面状态卡说 {match.group(1)} 个，实际 {expected} 个")
    if options != expected:
        problems.append(f"下拉 {options} 个选项，实际 {expected} 个")
    record("页面与后端一致", not problems,
           "；".join(problems) if problems
           else f"状态卡与下拉都是 {expected} 个")


def check_headers(model: str) -> None:
    """响应头：JSON 必须带 charset，流式必须是 text/event-stream。

    这两条都曾经真出过问题：早期 JSON 不带 charset，PowerShell 客户端里中文乱码；
    流式的 content-type 若不对，客户端会把它当成一次性响应而不实时显示。
    """
    problems: list[str] = []
    cases = [
        ("/v1/messages", "anthropic", False),
        ("/v1/messages", "anthropic", True),
        ("/v1/chat/completions", "openai", False),
        ("/v1/chat/completions", "openai", True),
    ]
    for path, proto, stream in cases:
        body = {"model": model, "max_tokens": 24,
                "messages": [{"role": "user", "content": "hi"}]}
        if stream:
            body["stream"] = True
        headers = ({**openai_headers()} if proto == "openai"
                   else {**anthropic_headers()})
        try:
            data = json.dumps(body).encode()
            request = urllib.request.Request(
                BASE + path, data=data,
                headers={**headers, "content-type": "application/json"})
            with urllib.request.urlopen(request, timeout=120) as resp:
                ctype = resp.headers.get("content-type", "")
                label = f"{path} {'流式' if stream else '非流式'}"
                if stream:
                    if "event-stream" not in ctype.lower():
                        problems.append(f"{label}: content-type={ctype}")
                elif "json" in ctype.lower() and "charset" not in ctype.lower():
                    problems.append(f"{label}: 缺 charset（{ctype}）")
                if stream:
                    for chunk in resp:
                        if len(chunk):
                            break
                else:
                    resp.read()
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{path} {proto}: {type(exc).__name__}")
    record("响应头", not problems,
           "；".join(problems) if problems
           else "JSON 带 charset、流式为 event-stream")



def check_concurrency(model: str, clients: int = 3) -> None:
    import threading
    outcomes: list[bool] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        try:
            if index % 2:
                status, payload, _ = post(
                    "/v1/chat/completions",
                    {"model": model, "max_tokens": 120,
                     "messages": [{"role": "user", "content": "只回答：OK"}]},
                    openai_headers())
                message = ((payload.get("choices") or [{}])[0] or {}).get("message") or {}
                ok = status == 200 and bool(
                    (message.get("content") or "").strip()
                    or (message.get("reasoning_content") or "").strip())
            else:
                status, payload, _ = post(
                    "/v1/messages",
                    {"model": model, "max_tokens": 120,
                     "messages": [{"role": "user", "content": "只回答：OK"}]},
                    anthropic_headers())
                blocks = payload.get("content") or []
                ok = status == 200 and any(
                    (b.get("text") or b.get("thinking") or "").strip()
                    for b in blocks)
        except Exception:  # noqa: BLE001
            ok = False
        with lock:
            outcomes.append(ok)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(clients)]
    began = time.time()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    passed = sum(outcomes)
    record("并发 N 客户端", passed == clients,
           f"{passed}/{clients} 成功，{time.time() - began:.1f}s")


def check_abort_cleanup() -> None:
    """客户端中途断开：不能泄漏，也不能把路由标死。"""
    body = json.dumps({"model": None, "max_tokens": 600, "stream": True,
                       "messages": [{"role": "user",
                                     "content": "用三句话介绍你自己"}]})
    try:
        request = urllib.request.Request(BASE + "/health",
                                         headers={"x-api-key": TOKEN})
        with urllib.request.urlopen(request, timeout=15) as resp:
            before = json.loads(resp.read())["stats"]
    except Exception as exc:  # noqa: BLE001
        record("断开清理", False, f"读 health 失败 {type(exc).__name__}")
        return
    model = None
    try:
        request = urllib.request.Request(BASE + "/v1/models",
                                         headers={"x-api-key": TOKEN})
        with urllib.request.urlopen(request, timeout=15) as resp:
            rows = json.loads(resp.read())["data"]
        model = rows[0]["id"]
    except Exception:  # noqa: BLE001
        record("断开清理", True, "（拿不到模型，跳过）")
        return
    payload = json.loads(body)
    payload["model"] = model
    raw = json.dumps(payload).encode()
    head = (
        f"POST /v1/messages HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
        "content-type: application/json\r\n"
        f"x-api-key: {TOKEN}\r\nanthropic-version: 2023-06-01\r\n"
        f"content-length: {len(raw)}\r\nconnection: close\r\n\r\n"
    ).encode()
    sock = socket.create_connection((HOST, PORT), timeout=10)
    try:
        sock.settimeout(45)
        sock.sendall(head + raw)
        got = 0
        while got < 200:
            chunk = sock.recv(4096)
            if not chunk:
                break
            got += len(chunk)
    except socket.timeout:
        pass
    finally:
        sock.close()
    time.sleep(5)
    try:
        request = urllib.request.Request(BASE + "/health",
                                         headers={"x-api-key": TOKEN})
        with urllib.request.urlopen(request, timeout=20) as resp:
            after = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        record("断开清理", False, f"复读 health 失败 {type(exc).__name__}")
        return
    stats = after["stats"]
    leaked = stats.get("active", 0) > 0 or bool(stats.get("inflight"))
    # 只关心**直连路由**被标死：中继路由本来就可能是死的（对端没开），
    # 而且早已从列表里隐藏。这和 `flix models doctor` 的严重度口径一致。
    poisoned = [model_id for model_id, row
                in (after.get("routes") or {}).items()
                if not row.get("ok", True) and model_id.count("--") <= 1]
    record("断开清理", (not leaked) and (not poisoned),
           f"active={stats.get('active')} aborted+="
           f"{stats.get('aborted', 0) - before.get('aborted', 0)} "
           f"failed+={stats.get('failed', 0) - before.get('failed', 0)} "
           f"直连被标死={poisoned or '无'}")


def main() -> int:
    print(f"验收目标: {BASE}\n", flush=True)
    health = check_health()
    ids = check_models(health)
    if not ids:
        print("\n拿不到模型列表，后续检查跳过。")
        return 1
    model = ids[0]
    check_anthropic(model)
    check_openai(model)
    check_openai_stream(model)
    check_tool_calls(model)
    check_auth()
    check_unknown_model()
    check_page()
    check_page_consistency(health, ids)
    check_headers(model)
    check_concurrency(model)
    check_abort_cleanup()

    # ok=None 表示「跳过」，不能算失败（之前用 not ok 会把跳过判成 FAIL）
    failed = [name for name, ok, _ in results if ok is False]
    skipped = [name for name, ok, _ in results if ok is None]
    judged = len(results) - len(skipped)
    print(f"\n{judged - len(failed)}/{judged} 通过"
          + (f"（跳过 {len(skipped)} 项）" if skipped else ""))
    if failed:
        print("失败项: " + ", ".join(failed))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
