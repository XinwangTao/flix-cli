"""LAN model gateway: one Anthropic-compatible endpoint for phones.

The phone only needs a base URL and a token; every request is forwarded
verbatim (including SSE streaming) to the local cc-switch proxy, so model
mapping, account routing and billing stay in exactly one place.

Wire contract (see docs/archify/flix_model_gateway_workflow.json):

- POST /v1/messages   -> proxied to <upstream>/v1/messages  (token in a header)
- GET  /v1/models     -> proxied (client health probe)
- GET  /health        -> {"ok": true}; full counters only with a valid token
- GET  /?token=<tok>  -> usage card for a phone browser (the only place a
                          query token is accepted; page is no-store)

Auth: `x-api-key` or `Authorization: Bearer`. The token is never accepted in
the query string of a proxied API call, so it cannot leak into upstream logs.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import html
import io
import json
import secrets
import subprocess
import sys
import time
from urllib.parse import quote, urlsplit

import aiohttp
from aiohttp import web

PREF_KEY = "modelGateway"
DEFAULT_PORT = 18993
DEFAULT_UPSTREAM = "http://127.0.0.1:15721"
DEFAULT_MAX_BODY_MB = 32
MAX_BODY_MB_CAP = 128
# 0 = no cap, and that is the default: a shared token decides *who* may call,
# not how many calls may be in flight. The upstream (a local cc-switch) already
# queues, and a client that streams several long requests is normal.
DEFAULT_MAX_CONCURRENT = 0
MAX_CONCURRENT_CAP = 32
BODY_READ_TIMEOUT = 30.0

CONFIG_TEMPLATE = {
    "enabled": False,
    "port": DEFAULT_PORT,
    "upstream": DEFAULT_UPSTREAM,
    "token": "",
    "max_body_mb": DEFAULT_MAX_BODY_MB,
    # 0 keeps the gateway from ever answering 429; set 1-32 to bound what one
    # token holder can occupy on the local cc-switch.
    "max_concurrent": DEFAULT_MAX_CONCURRENT,
    # Off by default: a reachable gateway that can be pointed anywhere is an
    # SSRF pivot for anyone holding the token.
    "allow_remote_upstream": False,
}

CFG = web.AppKey("cfg", dict)
STATS = web.AppKey("stats", dict)
SESSION = web.AppKey("session", aiohttp.ClientSession)

PASS_HEADERS = ("content-type", "request-id", "retry-after",
                "anthropic-ratelimit-requests-limit",
                "anthropic-ratelimit-requests-remaining",
                "anthropic-ratelimit-requests-reset",
                "anthropic-ratelimit-tokens-limit",
                "anthropic-ratelimit-tokens-remaining",
                "anthropic-ratelimit-tokens-reset")


def new_token() -> str:
    return "rd-" + secrets.token_urlsafe(24)


def mask_token(token: str) -> str:
    return f"{token[:6]}…" if token else ""


def _is_loopback(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    return host in ("localhost", "::1") or host.startswith("127.")


def normalize_upstream(url: str, allow_remote: bool = False) -> str:
    """Absolute http(s), no credentials, loopback unless explicitly allowed."""
    text = str(url or "").strip().rstrip("/")
    if not text:
        return DEFAULT_UPSTREAM
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"upstream 必须是 http:// 或 https:// 地址：{url!r}")
    if parts.username or parts.password:
        raise ValueError("upstream 不能带用户名/密码")
    if not _is_loopback(parts.hostname or "") and not allow_remote:
        raise ValueError(
            "upstream 只允许本机回环地址；要指向远端主机需显式开启"
            " allow_remote_upstream（CLI: --allow-remote-upstream）")
    return text


def _as_port(value) -> int:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_PORT
    return port if 1 <= port <= 65535 else DEFAULT_PORT


def _merged(raw: dict | None) -> dict:
    cfg = dict(CONFIG_TEMPLATE)
    if isinstance(raw, dict):
        for key in CONFIG_TEMPLATE:
            if key in raw:
                cfg[key] = raw[key]
    cfg["port"] = _as_port(cfg["port"])
    cfg["upstream"] = str(cfg["upstream"] or DEFAULT_UPSTREAM).rstrip("/")
    try:
        mib = int(cfg["max_body_mb"])
    except (TypeError, ValueError):
        mib = DEFAULT_MAX_BODY_MB
    cfg["max_body_mb"] = min(MAX_BODY_MB_CAP, max(1, mib))
    try:
        slots = int(cfg["max_concurrent"])
    except (TypeError, ValueError):
        slots = DEFAULT_MAX_CONCURRENT
    # 0 is meaningful here ("no cap"); only a positive value is an actual limit.
    cfg["max_concurrent"] = min(MAX_CONCURRENT_CAP, max(0, slots))
    cfg["allow_remote_upstream"] = bool(cfg["allow_remote_upstream"])
    return cfg


def load_config(store) -> dict:
    """Preferences merged over defaults; mints a token on first use."""
    cfg = _merged(store.prefs().get(PREF_KEY))
    if not cfg["token"]:
        cfg["token"] = new_token()
        store.set_pref(PREF_KEY, cfg)
    return cfg


def save_config(store, **changes) -> dict:
    cfg = load_config(store)
    cfg.update(changes)
    cfg = _merged(cfg)
    cfg["upstream"] = normalize_upstream(cfg["upstream"],
                                         cfg["allow_remote_upstream"])
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


_URL_CACHE: dict[str, object] = {"at": 0.0, "ips": []}
_URL_TTL = 60.0


def _cached_adapter_ipv4s() -> list[str]:
    """ipconfig costs ~100ms; the settings page asks on every switch."""
    now = time.monotonic()
    if now - float(_URL_CACHE["at"] or 0) < _URL_TTL and _URL_CACHE["ips"]:
        return list(_URL_CACHE["ips"])
    ips = _adapter_ipv4s()
    _URL_CACHE["at"], _URL_CACHE["ips"] = now, list(ips)
    return ips


def lan_urls(port: int, fresh: bool = False) -> list[str]:
    """Every private IPv4 this host could answer on, primary interface first.

    Phones usually sit on the Wi-Fi adapter while the wired NIC carries other
    traffic, so list all of them and let the caller pick.
    """
    from . import default_iface_ip

    ordered: list[str] = []
    primary = default_iface_ip()
    if primary:
        ordered.append(primary)
    ips = _adapter_ipv4s() if fresh else _cached_adapter_ipv4s()
    for ip in ips:
        if ip not in ordered:
            ordered.append(ip)
    live = [ip for ip in ordered
            if not ip.startswith(("127.", "169.254."))]
    return [f"http://{ip}:{port}" for ip in live] or [f"http://127.0.0.1:{port}"]


def _port_open(port: int, timeout: float = 0.15) -> bool:
    """Cheap loopback connect; False means nothing listens (fail fast)."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def share_info(store) -> dict | None:
    """LAN endpoint + token for a pairing QR; None while sharing is off.

    A disabled gateway must not put a token on screen: the QR then carries
    pairing fields only and the caller tells the user to turn sharing on.
    """
    cfg = load_config(store)
    if not cfg.get("enabled"):
        return None
    info = {"base": lan_urls(cfg["port"])[0], "token": cfg["token"],
            "port": cfg["port"]}
    # Enabled but not listening is a real state (crashed or still starting);
    # say so instead of handing out a dead endpoint. A connection check first
    # keeps the common "down" case instant instead of waiting out the probe.
    info["listening"] = False
    if _port_open(cfg["port"]):
        try:
            info["listening"] = bool(probe(cfg["port"], cfg["token"],
                                           timeout=1.5).get("listening"))
        except Exception:  # noqa: BLE001 - a probe failure must not block the QR
            info["listening"] = False
    return info


def _token_from(request: web.Request, allow_query: bool = False) -> str:
    raw = request.headers.get("x-api-key", "")
    if raw:
        return raw.strip()
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if allow_query:
        return (request.query.get("token") or "").strip()
    return ""


def _authorized(request: web.Request, token: str,
                allow_query: bool = False) -> bool:
    if not token:
        return False
    return hmac.compare_digest(_token_from(request, allow_query).encode(),
                               token.encode())


def _error(status: int, kind: str, message: str,
           headers: dict | None = None) -> web.Response:
    return web.json_response(
        {"type": "error", "error": {"type": kind, "message": message}},
        status=status, headers=headers)


def _target_url(cfg: dict, request: web.Request) -> str:
    """Upstream URL; the query string is forwarded byte-for-byte."""
    url = cfg["upstream"] + request.path
    raw = request.rel_url.raw_query_string
    return f"{url}?{raw}" if raw else url


FORWARD_HEADERS = ("content-type", "accept", "anthropic-version",
                   "anthropic-beta", "anthropic-dangerous-direct-browser-access")


def _new_stats() -> dict:
    return {"requests": 0, "ok": 0, "rejected": 0, "failed": 0,
            "aborted": 0, "too_large": 0, "active": 0, "last": 0,
            "started": int(time.time()), "recent": []}


RECENT_LIMIT = 20


def _note_reject(stats: dict, kind: str, remote: str) -> None:
    """Keep the last few refusals so a failing phone can be diagnosed.

    Counters say "3 rejected"; they do not say whether it was a stale token or
    the concurrency cap, nor from which address.
    """
    recent = stats.setdefault("recent", [])
    recent.append({"t": int(time.time()), "kind": kind, "remote": remote or ""})
    del recent[:-RECENT_LIMIT]


async def _proxy(request: web.Request) -> web.StreamResponse:
    cfg = request.app[CFG]
    stats = request.app[STATS]
    if not _authorized(request, cfg["token"]):
        stats["rejected"] += 1
        _note_reject(stats, "401 token", request.remote)
        return _error(401, "authentication_error",
                      "invalid gateway token; set x-api-key to the token "
                      "shown in RelayDesk settings")
    # 0 = no cap: never answer 429. Only an explicitly configured positive limit
    # is enforced, and it is read before the body so a flood cannot each grab a
    # 32 MB buffer.
    limit = int(cfg.get("max_concurrent") or 0)
    if limit > 0 and stats["active"] >= limit:
        stats["rejected"] += 1
        _note_reject(stats, "429 busy", request.remote)
        return _error(429, "overloaded_error",
                      f"gateway busy ({stats['active']} in flight, limit {limit}); "
                      "retry shortly", headers={"Retry-After": "2"})
    # Reserve the slot before the next await: reading the body suspends this
    # coroutine, and two requests could otherwise both pass the guard at 0.
    stats["active"] += 1
    try:
        body = await asyncio.wait_for(request.read(), BODY_READ_TIMEOUT)
    except asyncio.TimeoutError:
        stats["active"] -= 1
        stats["failed"] += 1
        return _error(408, "timeout_error", "request body read timed out")
    except BaseException:
        stats["active"] -= 1
        raise
    headers = {h: request.headers[h] for h in FORWARD_HEADERS
               if h in request.headers}
    url = _target_url(cfg, request)
    stats["requests"] += 1
    stats["last"] = int(time.time())
    stream = None  # set once response headers are on the wire
    try:
        async with request.app[SESSION].request(
                request.method, url, data=body or None,
                headers=headers) as upstream:
            ctype = upstream.headers.get("content-type", "application/json")
            out_headers = {h: upstream.headers[h] for h in PASS_HEADERS
                           if h in upstream.headers}
            if "text/event-stream" in ctype:
                resp = web.StreamResponse(status=upstream.status,
                                          headers=out_headers)
                resp.headers.setdefault("Cache-Control", "no-cache")
                resp.headers.setdefault("X-Accel-Buffering", "no")
                await resp.prepare(request)
                stream = resp  # from here on, never return another Response
                try:
                    async for chunk in upstream.content.iter_any():
                        await resp.write(chunk)
                    await resp.write_eof()
                except (ConnectionResetError, BrokenPipeError):
                    # The phone locked or closed the app mid-stream; the
                    # upstream call is cancelled by the context manager.
                    stats["aborted"] += 1
                    return resp
                stats["ok"] += 1
                return resp
            payload = await upstream.read()
            if upstream.status >= 400:
                stats["failed"] += 1
            else:
                stats["ok"] += 1
            out_headers.pop("content-type", None)
            return web.Response(status=upstream.status, body=payload,
                                headers=out_headers,
                                content_type=ctype.split(";")[0].strip())
    except asyncio.TimeoutError:
        if stream is not None:
            stats["aborted"] += 1
            return stream
        stats["failed"] += 1
        return _error(504, "timeout_error", "upstream timed out")
    except aiohttp.ClientError as exc:
        if stream is not None:
            # Headers already sent: writing a second response here would
            # corrupt the chunked stream, so close and report by disconnect.
            stats["aborted"] += 1
            with contextlib.suppress(Exception):
                await stream.write_eof()
            return stream
        stats["failed"] += 1
        _note_reject(stats, "502 upstream", request.remote)
        return _error(502, "api_error",
                      f"upstream {cfg['upstream']} unreachable: "
                      f"{type(exc).__name__}")
    finally:
        stats["active"] -= 1


@web.middleware
async def _too_large(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPRequestEntityTooLarge:
        stats = request.app.get(STATS)
        if isinstance(stats, dict):
            stats["too_large"] += 1
        return _error(413, "request_too_large",
                      "request body exceeds max_body_mb in RelayDesk settings")


async def _health(request: web.Request) -> web.Response:
    """Cheap liveness for anyone; details only for the token holder."""
    cfg = request.app[CFG]
    stats = dict(request.app[STATS])
    if not _authorized(request, cfg["token"]):
        return web.json_response({"ok": True})
    stats["uptime"] = max(0, int(time.time()) - stats.pop("started", 0))
    resp = web.json_response({"ok": True, "upstream": cfg["upstream"],
                              "port": cfg["port"], "stats": stats})
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
    cfg = request.app[CFG]
    if not _authorized(request, cfg["token"], allow_query=True):
        return web.Response(status=401, text="需要令牌：/?token=<令牌>",
                            content_type="text/plain")
    # Never trust the Host header: it is attacker-controlled and would be
    # echoed into the page the phone then copies.
    base = lan_urls(cfg["port"])[0] if cfg.get("port") else ""
    body = PAGE % {"base": html.escape(base), "token": html.escape(cfg["token"])}
    resp = web.Response(text=body, content_type="text/html")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def make_model_app(store, cfg: dict | None = None) -> web.Application:
    cfg = cfg or load_config(store)
    app = web.Application(
        middlewares=[_too_large],
        client_max_size=max(1, int(cfg.get("max_body_mb",
                                           DEFAULT_MAX_BODY_MB))) * 1024 * 1024)
    app[CFG] = cfg
    app[STATS] = _new_stats()

    async def _on_start(app: web.Application) -> None:
        app[SESSION] = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, connect=10,
                                          sock_read=900))

    async def _on_cleanup(app: web.Application) -> None:
        await app[SESSION].close()

    app.on_startup.append(_on_start)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_post("/v1/messages", _proxy)
    app.router.add_get("/v1/models", _proxy)
    app.router.add_get("/health", _health)
    app.router.add_get("/", _index)
    return app


async def run_gateway(store, port: int | None = None,
                      upstream: str | None = None,
                      allow_remote_upstream: bool | None = None,
                      ready=None) -> None:
    """Serve until cancelled; used by `flix models start` and `serve`."""
    changes = {}
    if port:
        changes["port"] = port
    if upstream:
        changes["upstream"] = upstream
    if allow_remote_upstream is not None:
        changes["allow_remote_upstream"] = bool(allow_remote_upstream)
    cfg = save_config(store, **changes) if changes else load_config(store)
    app = make_model_app(store, cfg)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", cfg["port"])
    await site.start()
    # The full token never goes to a log file; the GUI/CLI show it on demand.
    print(f"[models] gateway on http://0.0.0.0:{cfg['port']} -> "
          f"{cfg['upstream']}  token: {mask_token(cfg['token'])}", flush=True)
    if ready:
        ready(cfg)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


class GatewayHost:
    """The gateway listener owned by the daemon, toggleable while it runs.

    `flix models stop` used to only flip `enabled` in the prefs and print a note:
    port 18993 kept listening and requests kept flowing until the daemon was
    restarted, which is the opposite of what "关闭共享" promises (and the token
    stays valid LAN-wide). Owning the AppRunner lets the desktop API close and
    reopen it on demand.
    """

    def __init__(self, store):
        self.store = store
        self._runner = None
        self._cfg = None
        self.port = 0
        self.upstream = ""

    @property
    def listening(self) -> bool:
        return self._runner is not None

    def sync_config(self) -> dict:
        """Push pref changes (token/upstream/limits) into the live app.

        The app closes over one config dict, so a rotated token used to keep the
        OLD value working (and reject the new one) until the daemon restarted —
        a "revoke" that revokes nothing. Settings that change the listener itself
        (the port) still need stop/start.
        """
        cfg = load_config(self.store)
        if self._cfg is not None:
            for key in ("token", "upstream", "max_concurrent", "max_body_mb",
                        "allow_remote_upstream"):
                if key in cfg:
                    self._cfg[key] = cfg[key]
            self.upstream = str(self._cfg.get("upstream") or self.upstream)
        return cfg

    async def start(self, port: int | None = None) -> dict:
        cfg = load_config(self.store)
        target = int(port or cfg["port"])
        if self._runner is not None:
            if target == self.port:
                # Same listener: apply the settings that do not move it. Without
                # this, `models start --upstream X` answered "upstream: X" (and
                # status echoed it) while the live app kept forwarding to the old
                # upstream — a config report that lies.
                self.sync_config()
                return {"ok": True, "listening": True, "port": self.port,
                        "upstream": self.upstream, "already": True}
            await self.stop()
        cfg["port"] = target
        runner = web.AppRunner(make_model_app(self.store, cfg))
        await runner.setup()
        try:
            await web.TCPSite(runner, "0.0.0.0", target).start()
        except (OSError, ValueError):
            # Keep no half-built runner around: the next enable must start clean.
            with contextlib.suppress(Exception):
                await runner.cleanup()
            raise
        self._runner, self._cfg = runner, cfg
        self.port, self.upstream = target, cfg["upstream"]
        return {"ok": True, "listening": True, "port": target,
                "upstream": cfg["upstream"]}

    async def stop(self) -> dict:
        runner, self._runner = self._runner, None
        self._cfg = None
        port = self.port
        self.port = 0
        if runner is not None:
            with contextlib.suppress(Exception):
                await runner.cleanup()
        return {"ok": True, "listening": False, "port": port}


def port_open(port: int, timeout: float = 0.2) -> bool:
    """Cheap liveness check for GUI painting; never does an HTTP round trip."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except (OSError, TypeError, ValueError):
        return False


def probe(port: int, token: str = "", timeout: float = 3.0) -> dict:
    """Status check used by CLI and GUI; full counters need the token."""
    import urllib.error
    import urllib.request
    url = f"http://127.0.0.1:{int(port)}/health"
    req = urllib.request.Request(url)
    if token:
        req.add_header("x-api-key", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"listening": True, **json.loads(resp.read().decode())}
    except urllib.error.URLError as exc:
        return {"listening": False, "detail": str(exc.reason)}
    except (OSError, ValueError) as exc:
        return {"listening": False, "detail": str(exc)}


def _listening_pids(port: int) -> list[int]:
    """PIDs listening on `port`; used to stop the gateway without pid files."""
    import re
    pids: list[int] = []
    if sys.platform.startswith("win"):
        try:
            out = subprocess.run(["netstat", "-ano"], capture_output=True,
                                 text=True, encoding="utf-8", errors="ignore",
                                 creationflags=getattr(subprocess,
                                                       "CREATE_NO_WINDOW", 0),
                                 timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return pids
        needle = f":{port}"
        for line in out.splitlines():
            if needle not in line or "LISTENING" not in line.upper():
                continue
            local = line.split()[1] if len(line.split()) > 1 else ""
            if not local.endswith(needle):
                continue
            tail = line.split()[-1]
            if tail.isdigit():
                pids.append(int(tail))
    else:
        try:
            out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                                 capture_output=True, text=True, timeout=10).stdout
            pids = [int(x) for x in re.findall(r"\d+", out)]
        except (OSError, subprocess.SubprocessError):
            pass
    return sorted(set(pids))


def _pid_command_line(pid: int) -> str:
    """Best-effort command line for a PID (Windows only, empty elsewhere)."""
    if not sys.platform.startswith("win"):
        return ""
    with contextlib.suppress(Exception):
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}')"
             ".CommandLine"],
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=15).stdout
        return (out or "").strip()
    return ""


def stop_gateway(port: int) -> dict:
    """Stop a standalone gateway on `port`; never kill the shared daemon.

    PID files go stale (restart, reuse by an unrelated process), so the port
    is the source of truth. A gateway hosted by `flix serve`/`flixd` owns the
    device channel too — killing it would take the LAN relay down, so that
    case is reported as `daemon_hosted` and left alone.
    """
    pids = _listening_pids(port)
    if not pids:
        return {"ok": True, "stopped": [], "detail": "not listening"}
    stopped, refused, hosted = [], [], []
    for pid in pids:
        image = ""
        if sys.platform.startswith("win"):
            with contextlib.suppress(Exception):
                out = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="ignore",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    timeout=10).stdout
                image = (out.split(",")[0].strip('"') if out else "").lower()
        if image and not (image.startswith("relaydesk") or
                          image.startswith("python")):
            refused.append({"pid": pid, "image": image})
            continue
        cmdline = _pid_command_line(pid)
        if cmdline and "models" not in cmdline.lower():
            # `serve` / `flixd` share the process with the device channel.
            hosted.append(pid)
            continue
        try:
            if sys.platform.startswith("win"):
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True,
                               creationflags=getattr(subprocess,
                                                     "CREATE_NO_WINDOW", 0),
                               timeout=15)
            else:
                import os
                import signal
                os.kill(pid, signal.SIGTERM)
            stopped.append(pid)
        except (OSError, subprocess.SubprocessError):
            refused.append({"pid": pid, "image": image or "?"})
    return {"ok": not refused, "stopped": stopped, "refused": refused,
            "daemon_hosted": hosted}


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
    return f"{base}/?token={quote(cfg['token'])}"


def status_payload(store) -> dict:
    cfg = load_config(store)
    live = probe(cfg["port"], cfg["token"])
    return {"ok": True, "enabled": bool(cfg["enabled"]), "port": cfg["port"],
            "upstream": cfg["upstream"], "token": cfg["token"],
            "max_body_mb": cfg["max_body_mb"],
            "max_concurrent": cfg.get("max_concurrent", DEFAULT_MAX_CONCURRENT),
            "allow_remote_upstream": cfg["allow_remote_upstream"],
            "urls": lan_urls(cfg["port"]), "pair": pair_text(cfg),
            "listening": live.get("listening", False),
            "stats": live.get("stats", {}),
            "detail": live.get("detail", "")}
