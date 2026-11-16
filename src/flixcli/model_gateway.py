"""LAN model gateway: one Anthropic-compatible endpoint for phones.

The phone only needs a base URL and a token; every request is forwarded
verbatim (including SSE streaming) to the local cc-switch proxy, so model
mapping, account routing and billing stay in exactly one place.

Wire contract (see docs/archify/flix_model_gateway_workflow.json):

- POST /v1/messages   -> proxied to <upstream>/v1/messages
- GET  /v1/models     -> proxied (health probe for clients)
- GET  /health        -> {"ok": true, "upstream": ...} for status checks
- GET  /?token=<tok>  -> usage card for a phone browser

Auth: `x-api-key` or `Authorization: Bearer <token>`; a `token` query
parameter is accepted for the usage page and stripped before forwarding.
"""
from __future__ import annotations

import asyncio
import hmac
import io
import json
import secrets
import subprocess
import sys
from urllib.parse import quote

import aiohttp
from aiohttp import web

PREF_KEY = "modelGateway"
DEFAULT_PORT = 18993
DEFAULT_UPSTREAM = "http://127.0.0.1:15721"

CONFIG_TEMPLATE = {
    "enabled": False,
    "port": DEFAULT_PORT,
    "upstream": DEFAULT_UPSTREAM,
    "token": "",
}


def new_token() -> str:
    return "rd-" + secrets.token_urlsafe(24)


def load_config(store) -> dict:
    """Preferences merged over defaults; mints a token on first use."""
    raw = store.prefs().get(PREF_KEY)
    cfg = dict(CONFIG_TEMPLATE)
    if isinstance(raw, dict):
        for key in CONFIG_TEMPLATE:
            if key in raw:
                cfg[key] = raw[key]
    cfg["port"] = int(cfg["port"] or DEFAULT_PORT)
    cfg["upstream"] = str(cfg["upstream"] or DEFAULT_UPSTREAM).rstrip("/")
    if not cfg["token"]:
        cfg["token"] = new_token()
        store.set_pref(PREF_KEY, cfg)
    return cfg


def save_config(store, **changes) -> dict:
    cfg = load_config(store)
    cfg.update(changes)
    cfg["port"] = int(cfg["port"] or DEFAULT_PORT)
    cfg["upstream"] = str(cfg["upstream"] or DEFAULT_UPSTREAM).rstrip("/")
    store.set_pref(PREF_KEY, cfg)
    return cfg


def reset_token(store) -> str:
    cfg = save_config(store, token=new_token())
    return cfg["token"]


def _adapter_ipv4s() -> list[str]:
    """All IPv4s Windows currently has, including adapters marked deprecated.

    `local_ipv4s()` goes through GetAdaptersInfo, which drops adapters the
    kernel no longer prefers (a Wi-Fi NIC that lost its route but still holds
    the static address phones use). ipconfig lists them, so merge it in.
    """
    from . import local_ipv4s

    ips = list(local_ipv4s())
    if not sys.platform.startswith("win"):
        return ips
    try:
        out = subprocess.run(["ipconfig"], capture_output=True, text=True,
                             encoding="utf-8", errors="ignore",
                             creationflags=getattr(subprocess,
                                                   "CREATE_NO_WINDOW", 0),
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ips
    for line in out.splitlines():
        if "IPv4" not in line:
            continue
        for token in line.replace(":", " ").split():
            parts = token.split(".")
            if len(parts) == 4 and all(p.isdigit() for p in parts):
                ip = token
                if ip not in ips and not ip.startswith(("127.", "169.254.")):
                    ips.append(ip)
    return ips


def lan_urls(port: int) -> list[str]:
    """Every private IPv4 this host could answer on, primary interface first.

    Phones usually sit on the Wi-Fi adapter while the wired NIC carries other
    traffic, so list all of them and let the caller pick.
    """
    from . import default_iface_ip

    ordered: list[str] = []
    primary = default_iface_ip()
    if primary:
        ordered.append(primary)
    for ip in _adapter_ipv4s():
        if ip not in ordered:
            ordered.append(ip)
    ips = [ip for ip in ordered
           if not ip.startswith(("127.", "169.254."))]
    return [f"http://{ip}:{port}" for ip in ips] or [f"http://127.0.0.1:{port}"]


def _token_from(request: web.Request) -> str:
    raw = request.headers.get("x-api-key", "")
    if raw:
        return raw.strip()
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.query.get("token") or "").strip()


def _authorized(request: web.Request, token: str) -> bool:
    if not token:
        return False
    return hmac.compare_digest(_token_from(request).encode(),
                               token.encode())


def _error(status: int, kind: str, message: str) -> web.Response:
    return web.json_response(
        {"type": "error", "error": {"type": kind, "message": message}},
        status=status)


def _target_url(cfg: dict, request: web.Request) -> str:
    """Upstream URL for this request; drops our own `token` query parameter."""
    path = request.path
    query = [(k, v) for k, v in request.query.items() if k != "token"]
    url = cfg["upstream"] + path
    if query:
        url += "?" + "&".join(f"{quote(k)}={quote(v)}" for k, v in query)
    return url


FORWARD_HEADERS = ("content-type", "accept", "anthropic-version",
                   "anthropic-beta", "anthropic-dangerous-direct-browser-access")


async def _proxy(request: web.Request) -> web.StreamResponse:
    cfg = request.app["model_cfg"]
    if not _authorized(request, cfg["token"]):
        request.app["stats"]["rejected"] += 1
        return _error(401, "authentication_error",
                      "invalid gateway token; set x-api-key to the token "
                      "shown in RelayDesk settings")
    body = await request.read()
    headers = {h: request.headers[h] for h in FORWARD_HEADERS
               if h in request.headers}
    url = _target_url(cfg, request)
    request.app["stats"]["requests"] += 1
    request.app["stats"]["active"] += 1
    try:
        async with request.app["session"].request(
                request.method, url, data=body or None,
                headers=headers) as upstream:
            ctype = upstream.headers.get("content-type", "application/json")
            if "text/event-stream" in ctype:
                resp = web.StreamResponse(
                    status=upstream.status,
                    headers={"Content-Type": ctype,
                             "Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})
                await resp.prepare(request)
                async for chunk in upstream.content.iter_any():
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
            payload = await upstream.read()
            return web.Response(status=upstream.status, body=payload,
                                content_type=ctype.split(";")[0].strip())
    except asyncio.TimeoutError:
        request.app["stats"]["failed"] += 1
        return _error(504, "timeout_error", "upstream timed out")
    except aiohttp.ClientError as exc:
        request.app["stats"]["failed"] += 1
        return _error(502, "api_error",
                      f"upstream {cfg['upstream']} unreachable: "
                      f"{type(exc).__name__}")
    finally:
        request.app["stats"]["active"] -= 1


async def _health(request: web.Request) -> web.Response:
    cfg = request.app["model_cfg"]
    return web.json_response({"ok": True, "upstream": cfg["upstream"],
                              "port": cfg["port"],
                              "stats": dict(request.app["stats"])})


PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RelayDesk 模型共享</title>
<style>body{font-family:system-ui;background:#111;color:#eee;margin:0;padding:24px;
line-height:1.7}code{background:#222;padding:2px 6px;border-radius:4px;
word-break:break-all}.box{border:1px solid #333;border-radius:10px;padding:16px;
margin:16px 0}button{padding:10px 18px;border:0;border-radius:6px;background:#0a84ff;
color:#fff;font-size:15px}</style></head><body>
<h2>RelayDesk 模型共享</h2>
<div class="box"><div>协议：<code>Anthropic Messages</code></div>
<div>Base URL：<code id="base">%(base)s</code></div>
<div>API Key：<code id="tok">%(token)s</code></div>
<div>模型：<code>claude-sonnet-4-5</code> / <code>claude-opus-4-1</code> /
<code>claude-haiku-4-5</code> / <code>claude-fable-5</code></div></div>
<button onclick="navigator.clipboard.writeText(document.getElementById('base').
textContent);this.textContent='已复制'">复制地址</button>
<p style="opacity:.6">把 Base URL 和 API Key 填进手机 App 即可；本页不代理别的接口。</p>
</body></html>"""


async def _index(request: web.Request) -> web.Response:
    cfg = request.app["model_cfg"]
    if not _authorized(request, cfg["token"]):
        return web.Response(status=401, text="需要令牌：/?token=<令牌>",
                            content_type="text/plain")
    base = f"http://{request.host}"
    html = PAGE % {"base": base, "token": cfg["token"]}
    return web.Response(text=html, content_type="text/html")


def make_model_app(store, cfg: dict | None = None) -> web.Application:
    cfg = cfg or load_config(store)
    app = web.Application()
    app["model_cfg"] = cfg
    app["stats"] = {"requests": 0, "rejected": 0, "failed": 0, "active": 0}

    async def _on_start(app: web.Application) -> None:
        app["session"] = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, connect=10,
                                          sock_read=600))

    async def _on_cleanup(app: web.Application) -> None:
        await app["session"].close()

    app.on_startup.append(_on_start)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_post("/v1/messages", _proxy)
    app.router.add_get("/v1/models", _proxy)
    app.router.add_get("/health", _health)
    app.router.add_get("/", _index)
    return app


async def run_gateway(store, port: int | None = None,
                      upstream: str | None = None,
                      ready=None) -> None:
    """Serve until cancelled; used by `flix models start` and `serve`."""
    changes = {}
    if port:
        changes["port"] = port
    if upstream:
        changes["upstream"] = upstream
    cfg = save_config(store, **changes) if changes else load_config(store)
    app = make_model_app(store, cfg)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", cfg["port"])
    await site.start()
    print(f"[models] gateway on http://0.0.0.0:{cfg['port']} -> "
          f"{cfg['upstream']}  token: {cfg['token']}", flush=True)
    if ready:
        ready(cfg)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


def port_open(port: int, timeout: float = 0.2) -> bool:
    """Cheap liveness check for GUI painting; never does an HTTP round trip."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def probe(port: int, timeout: float = 3.0) -> dict:
    """Non-blocking status check used by CLI and GUI."""
    import urllib.error
    import urllib.request
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return {"listening": True, **json.loads(resp.read().decode())}
    except urllib.error.URLError as exc:
        return {"listening": False, "detail": str(exc.reason)}
    except (OSError, ValueError) as exc:
        return {"listening": False, "detail": str(exc)}


def qr_png(text: str, scale: int = 6) -> bytes:
    """PNG bytes for a QR code; segno is a tiny pure-python dependency."""
    import segno
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=scale,
                                     border=2, dark="#111111",
                                     light="#ffffff")
    return buf.getvalue()


def pair_text(cfg: dict) -> str:
    """What the QR encodes: open the usage card, token included."""
    base = lan_urls(cfg["port"])[0]
    return f"{base}/?token={cfg['token']}"


def status_payload(store) -> dict:
    cfg = load_config(store)
    live = probe(cfg["port"])
    return {"ok": True, "enabled": bool(cfg["enabled"]), "port": cfg["port"],
            "upstream": cfg["upstream"], "token": cfg["token"],
            "urls": lan_urls(cfg["port"]), "pair": pair_text(cfg),
            "listening": live.get("listening", False),
            "detail": live.get("detail", "")}
