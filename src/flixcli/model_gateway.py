"""LAN model gateway: one Anthropic-compatible endpoint for phones.

The phone only needs a base URL and a token; every request is forwarded
(including SSE streaming) to the local cc-switch proxy, so model mapping,
account routing and billing stay in exactly one place.

Wire contract (see docs/archify/flix_model_gateway_workflow.json):

- POST /v1/messages   -> proxied to <upstream>/v1/messages  (token in a header)
- GET  /v1/models     -> Anthropic-shaped model catalog (what a phone picker reads)
- GET  /health        -> {"ok": true}; full counters only with a valid token
- GET  /?token=<tok>  -> phone page: config + live model list + self-test
                          (the only place a query token is accepted; no-store)

Auth: `x-api-key` or `Authorization: Bearer`. The token is never accepted in
the query string of a proxied API call, so it cannot leak into upstream logs.

Two things earned by real phone testing:

* cc-switch only answers the *Claude* catalog when the request carries
  `anthropic-version`; without it the same URL returns the ~220 KB Codex
  catalog (``{"models": [...]}`` with full base_instructions), which no
  Anthropic client can read. The gateway therefore always supplies the
  header, and normalises whatever comes back to ``{"data": [...]}``.
* Model ids in the stack are prefixed (``ccs-claude-<route>--<model>``) and a
  bare or legacy ``claude-*`` name silently fell back to a default model.
  Requests are resolved against the live catalog so the phone gets the model
  it asked for; legacy names from the old direct-cc-switch setup still work.
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
# The upstream (cc-switch) has its own small concurrency budget — measured:
# "Limit: 5 concurrent requests, queue: 15", answering 429 with an HTML page
# beyond that. Firing every phone/peer request straight through therefore
# breaks as soon as a few devices chat at once. We throttle *ourselves* to what
# the upstream accepts and let the rest wait in our queue instead of failing.
DEFAULT_UPSTREAM_CONCURRENCY = 5
UPSTREAM_CONCURRENCY_CAP = 64
DEFAULT_QUEUE_WAIT_S = 60
# A dead route (peer relay hung, provider unreachable) otherwise leaves the
# phone spinning until its own client gives up. Bounded here, so the gateway
# answers with a clear 504 and tries the next route for the same model name.
DEFAULT_UPSTREAM_TIMEOUT_S = 180
UPSTREAM_TIMEOUT_MIN = 10
UPSTREAM_TIMEOUT_MAX = 900
# Reading the request body is bounded per chunk (a stalled phone), not in
# total: a 20 MB image over a slow Wi-Fi segment is legitimate, it just must
# keep flowing. A fixed total timeout used to turn those into 408s.
BODY_IDLE_TIMEOUT = 20.0
BODY_CHUNK = 256 * 1024
# cc-switch routes by protocol: this header (not the path) decides whether
# /v1/models answers the Claude catalog or the Codex one.
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
MODEL_CACHE_TTL = 60.0
MODEL_CACHE_RETRY = 15.0
RECENT_REQUESTS_LIMIT = 30

# Names the phone used to type when it talked to cc-switch directly. Keeping
# them meaningful is cheap and means an old phone config keeps working; each
# one resolves against the live catalog, so the mapping cannot drift again the
# way the previously hardcoded list did (every entry ended up answering 503).
LEGACY_MODEL_ALIASES = {
    "claude-sonnet-4-5": "glm-5.3",
    "claude-opus-4-1": "glm-5.3-flash",
    "claude-haiku-4-5": "kimi-k2.7-code",
    "claude-fable-5": "deepseek-v4.1-flash",
}

# A phone browser preflights these before the usage card can call the API;
# without them the request never leaves the WebView.
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": ("content-type, x-api-key, authorization, "
                                    "anthropic-version, anthropic-beta, "
                                    "anthropic-dangerous-direct-browser-access"),
    "Access-Control-Max-Age": "86400",
}

# A wrong token is the one unauthenticated path left, so a LAN neighbour
# guessing it must not be free to hammer the gateway. Per-source window,
# not a global lockout: one noisy host must not lock out the real phone.
AUTH_FAIL_LIMIT = 10
AUTH_FAIL_WINDOW = 60.0
AUTH_BLOCK_SECONDS = 30

# Anthropic stop_reason -> OpenAI finish_reason.
FINISH_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "pause_turn": "stop",
    "refusal": "content_filter",
}

CONFIG_TEMPLATE = {
    "enabled": False,
    "port": DEFAULT_PORT,
    "upstream": DEFAULT_UPSTREAM,
    "token": "",
    "max_body_mb": DEFAULT_MAX_BODY_MB,
    # 0 keeps the gateway from ever answering 429; set 1-32 to bound what one
    # token holder can occupy on the local cc-switch.
    "max_concurrent": DEFAULT_MAX_CONCURRENT,
    # In-flight calls we allow against the upstream at once (0 = unlimited).
    # Extra requests queue here for up to queue_wait_s instead of the upstream
    # rejecting them.
    "upstream_concurrency": DEFAULT_UPSTREAM_CONCURRENCY,
    "queue_wait_s": DEFAULT_QUEUE_WAIT_S,
    # 已知不通的中继路由默认不出现在模型列表里：手机/对端的模型选择器只该看到
    # 现在真能用的。10 分钟窗口过后会重新出现并被探测一次，仍是死路再隐藏。
    "hide_dead_relays": True,
    # Off by default: a reachable gateway that can be pointed anywhere is an
    # SSRF pivot for anyone holding the token.
    "allow_remote_upstream": False,
    # Token to present to the upstream when it is itself a RelayDesk gateway
    # (LAN cascading: a machine without VPN points its own gateway at a peer
    # that has one). Our client-facing token is never forwarded.
    "upstream_token": "",
    # Whole-call budget for a non-streaming request; a stream is bounded per
    # read instead (a long answer must be allowed to finish).
    "upstream_timeout_s": DEFAULT_UPSTREAM_TIMEOUT_S,
}

CFG = web.AppKey("cfg", dict)
STATS = web.AppKey("stats", dict)
SESSION = web.AppKey("session", aiohttp.ClientSession)
WARM = web.AppKey("warm", object)
# Throttles in-flight upstream calls to what cc-switch can take; requests wait
# here instead of being rejected by the upstream.
UPSTREAM_GATE = web.AppKey("upstream_gate", object)

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


# -- model catalog -----------------------------------------------------------
# The upstream's catalog is the only source of truth for model ids: the stack
# prefixes them per route (``ccs-claude-<key>--<model>``), so both the phone
# picker and the alias resolver must read it instead of hardcoding names.

def _empty_catalog() -> dict:
    return {"ids": [], "by_id": {}, "short": {}, "candidates": {},
            "display": {}, "at": 0.0}


def _short_model(model_id: str) -> str:
    """``ccs-claude-a--ccs-claude-b--glm-5.3`` -> ``glm-5.3``."""
    text = str(model_id or "")
    return text.rsplit("--", 1)[-1] if "--" in text else text


def _parse_catalog(payload) -> dict:
    """Accept both upstream shapes; a phone only ever sees the Anthropic one.

    Claude catalog: ``{"data": [{"id", "display_name", ...}], "has_more"}``
    Codex catalog: ``{"models": [{"slug", "display_name", ...}]}``
    """
    catalog = _empty_catalog()
    if not isinstance(payload, dict):
        return catalog
    rows = payload.get("data")
    if not isinstance(rows, list):
        rows = payload.get("models")
    if not isinstance(rows, list):
        return catalog
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = str(row.get("id") or row.get("slug") or "").strip()
        if not model_id or model_id in catalog["by_id"]:
            continue
        display = str(row.get("display_name") or row.get("description")
                      or model_id).strip()
        catalog["ids"].append(model_id)
        catalog["by_id"][model_id] = row
        catalog["display"][model_id] = display
        # First occurrence wins on purpose: the catalog is ordered by stack
        # priority, so a short name resolves to the route the user configured
        # first instead of an arbitrary duplicate.
        catalog["short"].setdefault(_short_model(model_id), model_id)
        # Every route serving the same short name: used to fall back when one
        # route is out of quota or its LAN relay is down.
        catalog["candidates"].setdefault(_short_model(model_id),
                                         []).append(model_id)
    return catalog


_CATALOG_CACHE: dict[str, object] = {"upstream": "", "catalog": None,
                                     "at": 0.0, "fail_at": 0.0}


def _cached_catalog(upstream: str) -> dict | None:
    if _CATALOG_CACHE["upstream"] == upstream and _CATALOG_CACHE["catalog"]:
        return _CATALOG_CACHE["catalog"]  # type: ignore[return-value]
    return None


def _store_catalog(upstream: str, catalog: dict) -> dict:
    _CATALOG_CACHE.update(upstream=upstream, catalog=catalog,
                          at=time.monotonic(), fail_at=0.0)
    return catalog


def _catalog_is_fresh(ttl: float) -> bool:
    at = float(_CATALOG_CACHE["at"] or 0.0)
    return bool(_CATALOG_CACHE["catalog"]) and (time.monotonic() - at) < ttl


def catalog_sync(cfg: dict, timeout: float = 4.0) -> dict:
    """Blocking catalog read for the CLI/GUI; never raises."""
    upstream = cfg["upstream"].rstrip("/")
    if _catalog_is_fresh(MODEL_CACHE_TTL):
        return _cached_catalog(upstream) or _empty_catalog()
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        upstream + "/v1/models",
        headers={"anthropic-version": DEFAULT_ANTHROPIC_VERSION,
                 "x-api-key": (cfg.get("upstream_token")
                                or "relaydesk")})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        _CATALOG_CACHE.update(fail_at=time.monotonic())
        return _cached_catalog(upstream) or _empty_catalog()
    return _store_catalog(upstream, _parse_catalog(payload))


async def catalog_async(cfg: dict, session: aiohttp.ClientSession,
                        timeout: float = 3.0, *, force: bool = False) -> dict:
    """Catalog for a request handler: cached, failure-tolerant, never blocks.

    orce=True 跳过 TTL 与失败退避，重新向上游要一次。只在「请求的模型不在
    缓存里」时用：用户在 cc-switch 刚加了模型，手机上立刻用会拿到 400「未知模型」，
    而目录其实几秒后就更新了 —— 这种「刚加完却说不存在」很让人困惑。
    """
    upstream = cfg["upstream"].rstrip("/")
    if not force:
        if _catalog_is_fresh(MODEL_CACHE_TTL):
            return _cached_catalog(upstream) or _empty_catalog()
        failed_at = float(_CATALOG_CACHE["fail_at"] or 0.0)
        if (_CATALOG_CACHE["upstream"] == upstream and failed_at
                and time.monotonic() - failed_at < MODEL_CACHE_RETRY):
            return _cached_catalog(upstream) or _empty_catalog()
    try:
        async with session.get(
                upstream + "/v1/models",
                headers={"anthropic-version": DEFAULT_ANTHROPIC_VERSION,
                         "x-api-key": (cfg.get("upstream_token")
                                        or "relaydesk")},
                timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            payload = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        _CATALOG_CACHE.update(fail_at=time.monotonic())
        return _cached_catalog(upstream) or _empty_catalog()
    return _store_catalog(upstream, _parse_catalog(payload))


def catalog_matches(model: str, catalog: dict) -> bool:
    """Whether a requested name can be routed to a catalog entry.

    Used to reject unknown names *before* the upstream silently substitutes a
    default model (the failure mode that made `glm-5.3-flash` answer as
    `dsv41`). Aliases and short names count as matches.
    """
    name = str(model or "").strip()
    if not name or not (catalog or {}).get("ids"):
        return False
    if name in (catalog.get("by_id") or {}):
        return True
    short_names = catalog.get("short") or {}
    if name in short_names:
        return True
    legacy = LEGACY_MODEL_ALIASES.get(name)
    if legacy and legacy in short_names:
        return True
    lowered = name.lower()
    return any(short.lower() == lowered for short in short_names)


def model_hint(catalog: dict, limit: int = 8) -> str:
    """Comma-separated short names for an error message, direct routes first."""
    seen: list[str] = []
    for model_id in visible_model_ids(catalog):
        short = _short_model(model_id)
        if short and short not in seen:
            seen.append(short)
        if len(seen) >= limit:
            break
    return "、".join(seen)


def resolve_model(model: str, catalog: dict) -> str:
    """Map what the phone typed onto a real upstream model id.

    Order: exact id -> short name (``glm-5.3``) -> legacy ``claude-*`` name ->
    case-insensitive short name -> unchanged. Unknown names are passed through
    so exotic model ids keep working; the upstream reports them itself.
    """
    candidates = resolve_candidates(model, catalog)
    return candidates[0] if candidates else str(model or "").strip()


async def _warm_relay_prefixes(app: web.Application, catalog: dict,
                              per_prefix_timeout: float = 8.0) -> None:
    """每个中继前缀各探一个代表模型，结果进路由健康缓存。

    并发发、失败不抛：启动阶段只做「摸底」，让 /v1/models 立刻收敛到可用集合。
    """
    representatives: list[str] = []
    seen_prefixes: set[str] = set()
    for model_id in catalog.get("ids") or []:
        prefix = _relay_prefix(model_id)
        if not prefix or prefix in seen_prefixes:
            continue
        seen_prefixes.add(prefix)
        representatives.append(model_id)
    if not representatives:
        return
    try:
        await asyncio.gather(*(
            _probe_route(app[CFG], app[SESSION], model_id,
                         timeout=per_prefix_timeout)
            for model_id in representatives),
            return_exceptions=True)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 摸底失败不影响启动
        pass


CATALOG_REFRESH_S = 90.0
# 本进程是否已经跑过一次中继探测。冷启动时它是 0，此时中继路由一条都没验证过，
# 不能让它们出现在模型列表里（实测重启后 1.6s 就能服务，但列表还是 39 个、含 28
# 个死中继，约 7 秒后才收敛；用户此时打开选择器必然踩坑）。
_WARM_STATE: dict[str, float] = {"done_at": 0.0}

# 上游目录回了非 JSON（中间代理插的错误页）的次数。只用于诊断，
# 不参与路由决策——出现在 /health 的 last_error 里就够定位了。
_MODELS_BAD_BODY: dict[str, int] = {}


async def _warm_catalog(app: web.Application) -> None:
    """Keep the catalog and the relay health fresh for the life of the process.

    The first pass runs at startup so /health and the phone page are correct
    immediately. It then repeats: a dead route is only skipped for ROUTE_RETRY_S
    (60 s), after which it becomes a candidate again — without a periodic
    re-probe the model list would flap between "clean" and "back to 39", and a
    relay that came back would never be noticed.
    """
    await asyncio.sleep(0)  # let the server finish binding first
    while True:
        try:
            await _warm_once(app)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a refresh must never kill the task
            pass
        finally:
            # 注意用 finally：即使目录拉取失败，也说明「已经试过了」，
            # 否则中继会被永久藏起来。
            _WARM_STATE["done_at"] = time.monotonic()
        await asyncio.sleep(CATALOG_REFRESH_S)


async def _warm_once(app: web.Application) -> None:
    try:
        catalog = await catalog_async(app[CFG], app[SESSION])
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - warm-up is best effort
        return
    stats = app.get(STATS)
    if catalog and catalog.get("ids"):
        # 顺带把每个中继前缀验一遍：不验的话，列表里每个前缀都要等用户真的
        # 点一次才被判定为死路，手机选择器会长期显示一堆用不了的模型。
        await _warm_relay_prefixes(app, catalog)
    if isinstance(stats, dict):
        if catalog and catalog.get("ids"):
            stats["upstream_ok"] = True
            stats["last_error"] = ""
        else:
            # Cold-start diagnosis: without this, a stopped cc-switch looks
            # like a healthy gateway with an empty model list.
            stats["upstream_ok"] = False
            stats["last_error"] = ("启动预热失败：拉取模型目录没有结果"
                                   "（cc-switch 未运行或未监听 15721？）")


# -- route health ------------------------------------------------------------
# A cc-switch stack can hold several routes for the same model name, and one
# of them can be dead (a LAN relay whose peer is hung, a provider out of
# quota). Learned from real answers: a dead route otherwise costs the phone a
# full client timeout on every try.
_ROUTE_HEALTH: dict[str, dict] = {}
# A relay route hangs because the *peer prefix* is broken (its cc-switch can
# list models but not generate). Remember per prefix, so the next alias request
# fails in milliseconds instead of waiting out another full timeout.
_RELAY_PREFIX_HEALTH: dict[str, float] = {}
ROUTE_HEALTH_TTL = 600.0
# 失败的路由不能一棍子打死 10 分钟：上游偶发过载/限流会把「好的」直连路由也标死，
# 之后所有人都收到 503（实测压测后就是如此）。过了这个窗口允许再试一次。
ROUTE_RETRY_S = 60.0
# 唯一候选刚失败过时的重试预算：短一点，既给自愈机会又不让手机久等。
DEGRADED_RETRY_S = 30.0
MAX_ROUTE_ATTEMPTS = 3
# A hung relay can eat the whole 180 s budget before it is judged dead. A
# 1-token probe reaches the same verdict in seconds, and its result is cached
# with the route health, so this cost is paid once per prefix per 10 minutes.
RELAY_PROBE_TIMEOUT = 8.0


def _relay_prefix(model_id: str) -> str:
    """``ccs-claude-cn222--ccs-claude-x--m`` -> ``ccs-claude-cn222--``."""
    text = str(model_id or "")
    if text.count("--") < 2:
        return ""
    return text.split("--", 1)[0] + "--"


def _relay_prefix_state(prefix: str) -> bool | None:
    failed_at = _RELAY_PREFIX_HEALTH.get(prefix)
    if failed_at is None:
        return None
    if time.monotonic() - failed_at > ROUTE_HEALTH_TTL:
        return None
    return False


def _route_mark(model_id: str, ok: bool, ms: int = 0) -> None:
    body = _ROUTE_HEALTH.setdefault(model_id, {})
    body["ok"] = bool(ok)
    body["at"] = time.monotonic()
    body["ms"] = int(ms)
    prefix = _relay_prefix(model_id)
    if prefix:
        if ok:
            _RELAY_PREFIX_HEALTH.pop(prefix, None)
        else:
            _RELAY_PREFIX_HEALTH[prefix] = time.monotonic()


def _route_dead_recently(model_id: str) -> bool:
    """刚刚失败过、还在退避窗口内（这种才值得跳过）。

    两种来源都要看：这条路由自己的失败记录，以及它所属中继前缀的失败记录
    （同前缀的兄弟模型没有自己的记录，但前缀死了它们一样不可用）。
    """
    now = time.monotonic()
    body = _ROUTE_HEALTH.get(model_id)
    if body and not body.get("ok"):
        age = now - float(body.get("at") or 0)
        if age <= ROUTE_RETRY_S:
            return True
        # 自己的退避已过；但若前缀刚死，仍然跳过
    prefix = _relay_prefix(model_id)
    if prefix and _relay_prefix_state(prefix) is False:
        failed_at = float(_RELAY_PREFIX_HEALTH.get(prefix) or 0.0)
        return (now - failed_at) <= ROUTE_RETRY_S
    return False


def _route_state(model_id: str) -> bool | None:
    """True/False when learned recently, None when unknown or stale."""
    body = _ROUTE_HEALTH.get(model_id)
    state = None
    if body and time.monotonic() - float(body.get("at") or 0) <= ROUTE_HEALTH_TTL:
        state = bool(body.get("ok"))
    if state is not False:
        prefix = _relay_prefix(model_id)
        if prefix and _relay_prefix_state(prefix) is False:
            return False
    return state


def route_health() -> dict:
    now = time.monotonic()
    out = {}
    for model_id, body in _ROUTE_HEALTH.items():
        age = int(now - float(body.get("at") or 0))
        if age > ROUTE_HEALTH_TTL:
            continue
        out[model_id] = {"ok": bool(body.get("ok")), "ms": body.get("ms", 0),
                         "age": age}
    return out


def relay_prefix_health() -> dict:
    now = time.monotonic()
    return {prefix: {"ok": False, "age": int(now - at)}
            for prefix, at in _RELAY_PREFIX_HEALTH.items()
            if now - at <= ROUTE_HEALTH_TTL}


def _route_priority(model_id: str) -> int:
    """0 best .. 4 worst: working local routes beat working relays, dead last.

    A healthy relay is still one network hop to another machine, and a local
    direct route is the cheapest path to a model. Only a *failed* direct route
    lets a relay overtake it.
    """
    state = _route_state(model_id)
    if state is False:
        return 4
    if _is_direct_route(model_id):
        return 0 if state is True else 1
    return 2 if state is True else 3


def _is_direct_route(model_id: str) -> bool:
    """One ``ccs-claude-<route>--`` prefix = this host's own provider.

    Two prefixes mean the request is relayed to another machine's cc-switch
    (``ccs-claude-<relay>--ccs-claude-...--model``); that hop can be down
    while the local direct route keeps working.
    """
    return str(model_id).count("--") <= 1


def resolve_candidates(model: str, catalog: dict) -> list[str]:
    """Every upstream id worth trying, best first. Empty when unresolved."""
    name = str(model or "").strip()
    if not name:
        return []
    rows = (catalog or {}).get("ids") or []
    if not rows:
        return [name]
    if name in (catalog.get("by_id") or {}):
        return [name]
    matches = list((catalog.get("candidates") or {}).get(name) or [])
    if not matches:
        legacy = LEGACY_MODEL_ALIASES.get(name)
        if legacy:
            matches = list((catalog.get("candidates") or {}).get(legacy) or [])
    if not matches:
        lowered = name.lower()
        for short_name, model_ids in (catalog.get("candidates") or {}).items():
            if short_name.lower() == lowered:
                matches = list(model_ids)
                break
    if not matches:
        return [name]  # unknown model: let the upstream explain it
    return _order_candidates(matches)


def _order_candidates(model_ids: list[str]) -> list[str]:
    """Best path first: working local route, local guess, working relay, dead."""
    ordered = sorted(enumerate(model_ids),
                     key=lambda pair: (_route_priority(pair[1]), pair[0]))
    return [model_id for _, model_id in ordered]


async def _probe_route(cfg: dict, session: aiohttp.ClientSession,
                       model_id: str,
                       timeout: float = RELAY_PROBE_TIMEOUT) -> bool:
    """One-token request to judge a relay route fast; result is cached."""
    try:
        async with session.post(
                cfg["upstream"].rstrip("/") + "/v1/messages",
                json={"model": model_id, "max_tokens": 1,
                      "messages": [{"role": "user", "content": "ping"}]},
                headers={"anthropic-version": DEFAULT_ANTHROPIC_VERSION,
                         "x-api-key": (cfg.get("upstream_token")
                                        or "relaydesk")},
                timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            await resp.read()
            ok = resp.status < 400
    except (aiohttp.ClientError, asyncio.TimeoutError):
        ok = False
    _route_mark(model_id, ok, int(timeout * 1000))
    return ok


async def _usable_targets(cfg: dict, session: aiohttp.ClientSession,
                          targets: list[str]) -> list[str]:
    """Drop known-dead routes and probe unverified relay routes cheaply.

    Direct routes are this host's own providers and are not probed: an extra
    round trip there would just add latency for a very unlikely failure. A
    relay route is a different machine and can hang, so it earns a probe.
    """
    kept: list[str] = []
    for target in targets:
        if _route_dead_recently(target):
            continue
        if _relay_prefix(target) and _route_state(target) is not True:
            # 中继：没验证过 / 退避窗口已过，都用 1-token 探测判定
            if not await _probe_route(cfg, session, target):
                continue
        kept.append(target)
    return kept


def _rewrite_model_body(body: bytes, requested: str,
                        resolved: str) -> bytes | None:
    """Swap the model field in a JSON body; None when the body is not JSON.

    Only called when the name actually changes, so the verbatim fast path
    (the common case) still forwards the client's exact bytes.
    """
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("model") != requested:
        return None
    payload["model"] = resolved
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _note_request(stats: dict, entry: dict) -> None:
    """Ring buffer of recent proxied calls: the phone-side debugging trail."""
    recent = stats.setdefault("recent_requests", [])
    recent.append(entry)
    del recent[:-RECENT_REQUESTS_LIMIT]


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
    try:
        upstream_slots = int(cfg["upstream_concurrency"])
    except (TypeError, ValueError):
        upstream_slots = DEFAULT_UPSTREAM_CONCURRENCY
    cfg["upstream_concurrency"] = min(UPSTREAM_CONCURRENCY_CAP,
                                      max(0, upstream_slots))
    try:
        queue_wait = int(cfg.get("queue_wait_s") or DEFAULT_QUEUE_WAIT_S)
    except (TypeError, ValueError):
        queue_wait = DEFAULT_QUEUE_WAIT_S
    cfg["queue_wait_s"] = max(5, min(900, queue_wait))
    cfg["hide_dead_relays"] = bool(cfg.get("hide_dead_relays", True))
    try:
        timeout_s = int(cfg["upstream_timeout_s"])
    except (TypeError, ValueError):
        timeout_s = DEFAULT_UPSTREAM_TIMEOUT_S
    cfg["upstream_timeout_s"] = min(UPSTREAM_TIMEOUT_MAX,
                                    max(UPSTREAM_TIMEOUT_MIN, timeout_s))
    cfg["allow_remote_upstream"] = bool(cfg["allow_remote_upstream"])
    cfg["upstream_token"] = str(cfg.get("upstream_token") or "").strip()
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


_NAME_CACHE: dict[str, object] = {"at": 0.0, "names": {}}


def _adapter_names() -> dict[str, str]:
    """ip -> adapter friendly name (Windows), so phones can tell NICs apart.

    The phone often needs a specific NIC (Wi-Fi vs a USB-tether segment); a
    bare list of "http://10.x... http://192.168.x..." leaves the user guessing.
    """
    if not sys.platform.startswith("win"):
        return {}
    now = time.monotonic()
    if now - float(_NAME_CACHE["at"] or 0) < _URL_TTL and _NAME_CACHE["names"]:
        return dict(_NAME_CACHE["names"])  # type: ignore[arg-type]
    names: dict[str, str] = {}
    current = ""
    for line in _ipconfig_text().splitlines():
        if not line.strip():
            continue
        if not line[0].isspace() and line.rstrip().endswith(":"):
            current = _short_iface_name(line.strip().rstrip(":"))
            continue
        if "IPv4" not in line:
            continue
        for token in line.replace(":", " ").split():
            parts = token.split(".")
            if len(parts) == 4 and all(p.isdigit() for p in parts):
                names[token] = current
    _NAME_CACHE["at"], _NAME_CACHE["names"] = now, names
    return names


def lan_urls_named(port: int, fresh: bool = False) -> list[dict]:
    """LAN URLs with a human hint per address: [{url, ip, interface}]."""
    names = _adapter_names()
    rows = []
    for url in lan_urls(port, fresh=fresh):
        ip = url.rsplit("//", 1)[-1].rsplit(":", 1)[0]
        rows.append({"url": url, "ip": ip,
                     "interface": names.get(ip, "")})
    return rows


_IFACE_PREFIXES = ("无线局域网适配器 ", "以太网适配器 ", "未知适配器 ",
                   "Wireless LAN adapter ", "Ethernet adapter ")


def _short_iface_name(name: str) -> str:
    """`无线局域网适配器 WLAN` -> `WLAN`; the prefix carries no information."""
    text = " ".join(str(name or "").split())
    for prefix in _IFACE_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


_IPCONFIG_CACHE: dict[str, object] = {"at": 0.0, "text": ""}


def _ipconfig_text() -> str:
    """`ipconfig` output decoded with the console codepage (Chinese Windows
    writes OEM cp936, not UTF-8; utf-8 turned adapter names into mojibake)."""
    if not sys.platform.startswith("win"):
        return ""
    now = time.monotonic()
    if _IPCONFIG_CACHE["text"] and now - float(
            _IPCONFIG_CACHE["at"] or 0) < _URL_TTL:
        return str(_IPCONFIG_CACHE["text"])
    import locale
    encodings = ("mbcs", locale.getpreferredencoding(False), "utf-8")
    for encoding in dict.fromkeys(encodings):
        try:
            out = subprocess.run(
                ["ipconfig"], capture_output=True, text=True,
                encoding=encoding, errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=5).stdout
        except (LookupError, OSError, subprocess.SubprocessError):
            continue
        _IPCONFIG_CACHE["at"], _IPCONFIG_CACHE["text"] = now, out
        return out
    return ""


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
    for line in _ipconfig_text().splitlines():
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


# 401 的说明。刻意不区分「令牌错」与「没带对格式」——那是安全信息；
# 只把支持的两种发送方式讲清楚。实测最常见的 401 不是令牌错，而是
# Authorization 里漏了 Bearer 前缀、或用 Azure 风格的 api-key 头。
AUTH_HINT = ("invalid gateway token. Send it either as "
             "x-api-key: <token> or as Authorization: Bearer <token> "
             "(a bare token without the Bearer prefix, or an api-key header, "
             "is not accepted). Copy the value from RelayDesk settings or the "
             "self-test page.")


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
    merged = dict(CORS_HEADERS)
    if headers:
        merged.update(headers)
    return web.json_response(
        {"type": "error", "error": {"type": kind, "message": message}},
        status=status, headers=merged)


def _target_url(cfg: dict, request: web.Request) -> str:
    """Upstream URL; the query string is forwarded byte-for-byte."""
    url = cfg["upstream"] + request.path
    raw = request.rel_url.raw_query_string
    return f"{url}?{raw}" if raw else url


FORWARD_HEADERS = ("content-type", "accept", "anthropic-version",
                   "anthropic-beta", "anthropic-dangerous-direct-browser-access")


def _declared_ctype(raw: str) -> str:
    """Pass the upstream media type through, but always declare UTF-8 for JSON.

    cc-switch answers ``application/json`` with no charset. JSON is UTF-8 by
    RFC 8259, so the bytes were right, but strict/legacy clients (Windows
    PowerShell 5.1's `Invoke-WebRequest` is one) fall back to Latin-1 and show
    Chinese answers as mojibake. Declaring the charset fixes every such client
    without changing a byte of the payload.
    """
    base = (raw or "").split(";")[0].strip() or "application/json"
    rest = (raw or "")[len(base):].strip()
    if "charset=" in rest.lower():
        return raw
    if base.lower() in ("application/json", "text/event-stream"):
        return f"{base}; charset=utf-8"
    return base


def _reject_kind(request: web.Request, stats: dict, remote: str) -> str:
    """`401 token` 太笼统：区分「压根没带」和「带了个错的/过期的」。

    现场排查时这两种的处置完全不同（客户端根本没配 vs 令牌轮换过期了）。
    这里只记前 6 位，足够分辨，也不至于把完整令牌写进日志。
    """
    presented = _token_from(request)
    if not presented:
        return "401 token(missing)"
    return f"401 token({presented[:6]}…)"


def _new_stats() -> dict:
    return {"requests": 0, "ok": 0, "rejected": 0, "failed": 0,
            "aborted": 0, "too_large": 0, "active": 0, "last": 0,
            "started": int(time.time()), "recent": [],
            "recent_requests": [], "upstream_ok": None, "last_error": "",
            "auth_fails": {}, "inflight": {}}


def _enter_active(stats: dict, request) -> str:
    """记录一个在途请求，并让 active 由登记表推导（自然自愈）。

    原来是裸的 += / -=，异常路径一旦漏减就会永久漂移（实测出现过 active 卡在 1）。
    现在以 inflight 登记表为准：任何收尾都会 pop，健康检查再按时间清理陈旧项。
    """
    key = f"{id(request)}"
    stats.setdefault("inflight", {})[key] = time.monotonic()
    stats["active"] = len(stats["inflight"])
    return key


def _leave_active(stats: dict, key: str) -> None:
    stats.setdefault("inflight", {}).pop(key, None)
    stats["active"] = len(stats["inflight"])


def _sweep_active(stats: dict, stale_after: float) -> None:
    """清掉超过 stale_after 还没收尾的登记项（进程被挂起/取消等极端情况）。"""
    inflight = stats.setdefault("inflight", {})
    now = time.monotonic()
    for key in [k for k, at in inflight.items() if now - float(at) > stale_after]:
        inflight.pop(key, None)
    stats["active"] = len(inflight)


def _auth_block_remaining(stats: dict, remote: str) -> int:
    """Seconds a source is locked out after too many bad tokens (0 = allowed)."""
    now = time.monotonic()
    hits = stats.setdefault("auth_fails", {})
    stamps = [t for t in hits.get(remote or "?", [])
              if now - t < AUTH_FAIL_WINDOW]
    hits[remote or "?"] = stamps
    if len(stamps) >= AUTH_FAIL_LIMIT:
        return AUTH_BLOCK_SECONDS
    return 0


def _auth_note_fail(stats: dict, remote: str) -> None:
    hits = stats.setdefault("auth_fails", {})
    now = time.monotonic()
    stamps = [t for t in hits.get(remote or "?", [])
              if now - t < AUTH_FAIL_WINDOW]
    stamps.append(now)
    hits[remote or "?"] = stamps
    if len(hits) > 64:  # a scan would otherwise grow this dict forever
        for key in list(hits)[:-32]:
            hits.pop(key, None)


def _auth_clear(stats: dict, remote: str) -> None:
    (stats.get("auth_fails") or {}).pop(remote or "?", None)


MODEL_CREATED = 1767225600  # 2026-01-01T00:00:00Z, stable "created" for pickers


def _model_entry(model_id: str, display: str) -> dict:
    """One model row that satisfies both client families.

    Anthropic clients read ``type``/``display_name``; OpenAI clients read
    ``object``/``owned_by``/``created``. Unknown extra fields are ignored by
    both, so one list serves a phone app of either protocol.
    """
    return {"type": "model", "object": "model", "id": model_id,
            "display_name": display, "owned_by": "cc-switch",
            "created": MODEL_CREATED}


def visible_model_ids(catalog: dict, hide_dead: bool = True,
                      pending_relays_hidden: bool = False) -> list[str]:
    """要给客户端看的模型 id：排序后，可选地剔掉已知不通的中继路由。

    一个栈里几条 LAN 中继全挂时，列表里 39 个有 28 个用不了——手机选择器里
    全是坑。默认隐藏它们；如果隐藏后一个不剩（极端情况），就全部保留，
    绝不返回空列表。
    """
    ids = list((catalog or {}).get("ids") or [])
    ordered = _order_model_ids(ids)
    if not hide_dead:
        return ordered
    if pending_relays_hidden and not _WARM_STATE["done_at"]:
        # 冷启动窗口（仅网关进程传这个标志）：一条中继都还没探测过，
        # 先只给直连路由，别让用户挑到死路。
        # 若这个栈根本没有直连路由（纯级联消费方），就退回完整列表——
        # 宁可列出未验证的，也不能给出空列表。
        direct_only = [m for m in ordered if not _relay_prefix(m)]
        if direct_only:
            ordered = direct_only
    # 隐藏窗口用 ROUTE_HEALTH_TTL（10 分钟），而不是选择用的退避窗口（60s）：
    # 退避到期后路由会被重试，但筛选器若跟着一起放开，列表就会在 11/39 之间
    # 反复跳（实测过）。重试一旦成功，_route_state 立刻变 True，模型马上回来。
    alive = [m for m in ordered if _route_state(m) is not False]
    return alive or ordered


def _order_model_ids(model_ids: list[str]) -> list[str]:
    """Best-first ordering for a phone picker: local routes, then relays, dead last.

    The upstream lists routes in stack order, and the relay route is often
    first while also being the one that hangs. A phone client preselects the
    first entry, so order is a behaviour, not cosmetics.
    """
    index_of = {model_id: i for i, model_id in enumerate(model_ids)}

    def score(model_id: str) -> tuple[int, int]:
        return (_route_priority(model_id), index_of.get(model_id, 0))

    return sorted(model_ids, key=score)


def _normalize_models_payload(payload: bytes) -> bytes:
    """Guarantee the catalog shape a phone picker can read.

    cc-switch answers ``{"data": [...]}`` when the request carried
    ``anthropic-version`` (the gateway always sends it) but ``{"models":
    [...]}` with full Codex instructions otherwise. Pass an already-correct
    payload through untouched so nothing is lost on the happy path.
    """
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        # 上游塞了非 JSON（中间代理的错误页）。原样透传会让手机的下拉框
        # 拿到一段 HTML 并解析失败；这里换成明确的「空目录」，客户端至少
        # 能正常渲染成「暂无模型」。详细原因在 /health 的 last_error 里。
        _MODELS_BAD_BODY["n"] = _MODELS_BAD_BODY.get("n", 0) + 1
        return json.dumps({"object": "list", "data": []}).encode()
    rows = data.get("data") if isinstance(data, dict) else None
    if isinstance(rows, list) and rows and all(
            isinstance(row, dict) and row.get("id") for row in rows):
        marked = any(_route_state(row["id"]) is False for row in rows)
        # OpenAI-only clients need two keys Anthropic omits; add them without
        # touching the rest of the upstream payload.
        needs_oai = not all("object" in row for row in rows)
        if not marked and not needs_oai:
            return payload
        by_id = {row["id"]: row for row in rows}
        ordered = visible_model_ids(
            _parse_catalog({"data": rows}),
            pending_relays_hidden=not _WARM_STATE["done_at"])
        out = dict(data)
        out["object"] = "list"
        out["data"] = [
            {**by_id[model_id],
             "object": by_id[model_id].get("object", "model"),
             "owned_by": by_id[model_id].get("owned_by", "cc-switch"),
             "created": by_id[model_id].get("created", MODEL_CREATED),
             "display_name": _display_with_health(
                 model_id, None,
                 by_id[model_id].get("display_name") or model_id)}
            for model_id in ordered]
        return json.dumps(out, ensure_ascii=False).encode("utf-8")
    catalog = _parse_catalog(data)
    if not catalog["ids"]:
        # 上游给了合法 JSON 但没有可识别的模型（`{}`、`{"data": []}`、或
        # 完全不认识的形状）。统一成标准的空列表：客户端期望 `data` 是数组，
        # 直接把 `{}` 透过去会让某些手机 App 的下拉框崩在解析上。
        return json.dumps({"object": "list", "data": []}).encode()
    ordered = visible_model_ids(
        catalog, pending_relays_hidden=not _WARM_STATE["done_at"])
    out = {
        "object": "list",
        "data": [_model_entry(model_id,
                              _display_with_health(model_id, catalog))
                 for model_id in ordered],
        "first_id": ordered[0],
        "last_id": ordered[-1],
        "has_more": False,
    }
    return json.dumps(out, ensure_ascii=False).encode("utf-8")


def _display_with_health(model_id: str, catalog: dict | None,
                         fallback: str | None = None) -> str:
    """Model name as the phone shows it, marked when this route last failed.

    The picker is where the user chooses; saying "当前不通" there is the
    cheapest way to stop them picking a route whose LAN relay is down.
    """
    display = ((catalog or {}).get("display") or {}).get(model_id)
    display = display or fallback or model_id
    if _route_state(model_id) is False:
        return f"{display}（当前不通）"
    return display


RECENT_LIMIT = 20


def _note_reject(stats: dict, kind: str, remote: str) -> None:
    """Keep the last few refusals so a failing phone can be diagnosed.

    Counters say "3 rejected"; they do not say whether it was a stale token or
    the concurrency cap, nor from which address.
    """
    recent = stats.setdefault("recent", [])
    recent.append({"t": int(time.time()), "kind": kind, "remote": remote or ""})
    del recent[:-RECENT_LIMIT]


class BodyTooLarge(Exception):
    """The request body exceeds max_body_mb."""


class BodyTimeout(Exception):
    """The client stopped sending the body mid-upload."""


class QueueFull(Exception):
    """Waiting for an upstream slot exceeded queue_wait_s."""


@contextlib.asynccontextmanager
async def _upstream_slot(request: web.Request, cfg: dict):
    """Hold one upstream concurrency slot, or fail after the queue budget.

    cc-switch accepts only ~5 concurrent requests and answers 429 (HTML) beyond
    that, which is exactly what breaks when several phones chat at once. Queuing
    here turns that burst into a short wait instead of a failed request.
    """
    gate = request.app.get(UPSTREAM_GATE)
    if gate is None:
        yield
        return
    wait_s = float(cfg.get("queue_wait_s") or DEFAULT_QUEUE_WAIT_S)
    try:
        await asyncio.wait_for(gate.acquire(), timeout=wait_s)
    except asyncio.TimeoutError as exc:
        raise QueueFull() from exc
    try:
        yield
    finally:
        gate.release()


def _body_limit_bytes(cfg: dict) -> int:
    try:
        mib = int(cfg.get("max_body_mb") or DEFAULT_MAX_BODY_MB)
    except (TypeError, ValueError):
        mib = DEFAULT_MAX_BODY_MB
    return max(1, mib) * 1024 * 1024


async def _read_body(request: web.Request, cfg: dict,
                     idle_timeout: float = BODY_IDLE_TIMEOUT) -> bytes:
    """Read the body with an idle timeout and a streaming size limit.

    `request.read()` applies one total timeout, so a large-but-healthy upload
    on a slow phone link used to fail as 408. Reading chunk by chunk means
    only a *stall* fails; a declared Content-Length over the limit is rejected
    before a single byte is buffered.
    """
    limit = _body_limit_bytes(cfg)
    declared = request.content_length
    if declared is not None and declared > limit:
        raise BodyTooLarge()
    chunks: list[bytes] = []
    total = 0
    stream = request.content.iter_chunked(BODY_CHUNK)
    while True:
        try:
            chunk = await asyncio.wait_for(stream.__anext__(), idle_timeout)
        except StopAsyncIteration:
            break
        except asyncio.TimeoutError as exc:
            raise BodyTimeout() from exc
        except (ConnectionResetError, aiohttp.ClientError) as exc:
            raise BodyTimeout() from exc
        total += len(chunk)
        if total > limit:
            raise BodyTooLarge()
        chunks.append(chunk)
    return b"".join(chunks)


async def _proxy(request: web.Request) -> web.StreamResponse:
    cfg = request.app[CFG]
    stats = request.app[STATS]
    started = time.monotonic()
    entry = {"t": int(time.time()), "path": request.path, "model": "",
             "to": "", "remote": request.remote or "", "stream": False,
             "status": 0, "ms": 0}
    reserved = False

    def fail(status: int, kind: str, message: str,
             headers: dict | None = None) -> web.Response:
        entry["status"] = status
        entry["error"] = kind
        return _error(status, kind, message, headers)

    try:
        blocked = _auth_block_remaining(stats, request.remote or "")
        if blocked:
            stats["rejected"] += 1
            _note_reject(stats, "429 auth-flood", request.remote)
            return fail(429, "rate_limit_error",
                        f"too many invalid tokens from this address; "
                        f"retry in {blocked}s",
                        headers={"Retry-After": str(blocked)})
        if not _authorized(request, cfg["token"]):
            stats["rejected"] += 1
            _auth_note_fail(stats, request.remote or "")
            _note_reject(stats, _reject_kind(request, stats, request.remote),
                         request.remote)
            return fail(401, "authentication_error", AUTH_HINT)
        _auth_clear(stats, request.remote or "")
        # 0 = no cap: never answer 429. Only an explicitly configured positive
        # limit is enforced, and it is read before the body so a flood cannot
        # each grab a 32 MB buffer.
        limit = int(cfg.get("max_concurrent") or 0)
        if limit > 0 and stats["active"] >= limit:
            stats["rejected"] += 1
            _note_reject(stats, "429 busy", request.remote)
            return fail(429, "overloaded_error",
                        f"gateway busy ({stats['active']} in flight, "
                        f"limit {limit}); retry shortly",
                        headers={"Retry-After": "2"})
        # Reserve the slot before the next await: reading the body suspends
        # this coroutine, and two requests could otherwise both pass at 0.
        active_key = _enter_active(stats, request)
        reserved = True
        try:
            body = await _read_body(request, cfg)
            # 客户端填错东西时要当场说清楚，别原样转给上游：
            # 实测缺 model 会拿到 cc-switch 的 503「No available accounts...」，
            # 用户被指引去「换账号」；非法 JSON 更会拿到 500（客户端错误却报服务端错）。
            if request.method == "POST" and (
                    request.path.rstrip("/") in ("/v1/messages",
                                                 "/v1/chat/completions")):
                if not body.strip():
                    stats["failed"] += 1
                    return fail(400, "invalid_request_error",
                                "请求体是空的：POST 需要 JSON body")
                try:
                    parsed = json.loads(body)
                except (ValueError, UnicodeDecodeError) as exc:
                    stats["failed"] += 1
                    return fail(400, "invalid_request_error",
                                f"请求体不是合法 JSON：{exc}")
                if not isinstance(parsed, dict):
                    stats["failed"] += 1
                    return fail(400, "invalid_request_error",
                                "请求体必须是 JSON 对象")
                if not isinstance(parsed.get("model"), str) or not parsed["model"]:
                    stats["failed"] += 1
                    stats["last_error"] = "client sent no model"
                    return fail(400, "invalid_request_error",
                                "缺少 model 字段（填模型短名或完整 id；"
                                "GET /v1/models 可列出可用项）")
        except BodyTooLarge:
            stats["too_large"] += 1
            return fail(413, "request_too_large",
                        "request body exceeds max_body_mb in RelayDesk settings")
        except BodyTimeout:
            stats["failed"] += 1
            return fail(408, "timeout_error",
                        "request body stalled (no data within "
                        f"{int(BODY_IDLE_TIMEOUT)}s)")
        headers = {h: request.headers[h] for h in FORWARD_HEADERS
                   if h in request.headers}
        # cc-switch picks the protocol by this header: without it /v1/models
        # answers the Codex catalog and the phone picker reads nothing.
        headers.setdefault("anthropic-version", DEFAULT_ANTHROPIC_VERSION)
        if cfg.get("upstream_token"):
            # Cascading: the upstream is another RelayDesk gateway and wants
            # *its* token. The client's token is ours and must not leak there.
            headers["x-api-key"] = cfg["upstream_token"]
        url = _target_url(cfg, request)
        stats["requests"] += 1
        stats["last"] = int(time.time())
        if request.method == "POST" and body:
            with contextlib.suppress(ValueError, UnicodeDecodeError):
                parsed = json.loads(body)
                if isinstance(parsed, dict) and isinstance(parsed.get("model"),
                                                           str):
                    entry["model"] = parsed["model"]
        wants_stream = False
        targets: list[str] = [""]
        if request.method == "POST" and body:
            with contextlib.suppress(ValueError, UnicodeDecodeError):
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    wants_stream = bool(parsed.get("stream"))
                    if isinstance(parsed.get("model"), str):
                        entry["model"] = parsed["model"]
        if entry["model"]:
            catalog = await catalog_async(cfg, request.app[SESSION])
            if catalog.get("ids") and not catalog_matches(entry["model"],
                                                          catalog):
                # 可能是用户刚在 cc-switch 里加了这个模型：强制重取一次再判，
                # 否则「刚加完就说不存在」会让人以为共享坏了。
                fresh = await catalog_async(cfg, request.app[SESSION], force=True)
                if fresh.get("ids"):
                    catalog = fresh
            if catalog.get("ids") and not catalog_matches(entry["model"],
                                                          catalog):
                # cc-switch would silently fall back to a default model here
                # (that is how `glm-5.3-flash` used to answer as `dsv41`), so
                # answering 400 with the real list is both honest and faster.
                stats["failed"] += 1
                stats["last_error"] = (f"unknown model {entry['model']}")
                return fail(400, "invalid_request_error",
                            f"未知模型 {entry['model']}：cc-switch 目录里没有。"
                            f"可用模型：{model_hint(catalog)}"
                            "（GET /v1/models 或 flix models list 看全部）")
            targets = (resolve_candidates(entry["model"], catalog)
                       or [entry["model"]])[:MAX_ROUTE_ATTEMPTS]
        if request.path.rstrip("/") != "/v1/messages":
            targets = targets[:1]  # only model calls get the fallback tour
        # Every candidate already failed recently: answering now is honest and
        # instant, instead of making the phone wait out another 180 s timeout.
        if (entry["model"] and targets
                and any(_relay_prefix(target) for target in targets)):
            targets = await _usable_targets(cfg, request.app[SESSION], targets)
        # 只剩一个候选、而它刚失败过时，**不能**快失败：快失败等于必定失败，
        # 而它可能只是抖了一下。给一次短预算的重试，自愈就不必等退避窗口。
        # （多个候选都死时仍然快失败——那说明模型本身此刻不可用，早说早换。）
        degraded = False
        if (entry["model"] and targets
                and all(_route_dead_recently(x) for x in targets)):
            if len(targets) > 1:
                targets = []
            else:
                degraded = True
        if entry["model"] and not targets:
            stats["failed"] += 1
            stats["last_error"] = (f"all routes for {entry['model']} known "
                                   "unreachable")
            # 区分两种原因，否则用户只会「等 10 分钟」，而实际是整条中继不通：
            #   a) 候选路由全挂在不可用的中继前缀上 -> 该换模型
            #   b) 路由本身刚失败过 -> 稍后自动重试
            candidates = (resolve_candidates(entry["model"], catalog)
                          or [entry["model"]])
            if candidates and all(_relay_prefix(t) for t in candidates):
                hint = (f"{entry['model']} 只在对端中继上，而那条中继当前不通。"
                        "换一个本机直连的模型（GET /v1/models 看现在能用的），"
                        "或等对端网关恢复")
            else:
                hint = (f"{entry['model']} 的候选路由近期都失败过，会自动重试；"
                        "也可以换一个模型试")
            return fail(503, "overloaded_error", f"没有可用路由：{hint}")
        timeout_s = float(cfg.get("upstream_timeout_s")
                          or DEFAULT_UPSTREAM_TIMEOUT_S)
        if degraded:
            timeout_s = min(timeout_s, DEGRADED_RETRY_S)
        # One budget for the whole call, shared by every route attempt: the
        # phone must not wait 3 × 25 s just because one relay is hung.
        deadline = time.monotonic() + timeout_s
        last_status = 0
        last_error = ""
        connect_retried: set[str] = set()
        for attempt, target in enumerate(targets):
            remaining = deadline - time.monotonic()
            if attempt and remaining <= 1.0:
                last_error = last_error or f"upstream timed out after {int(timeout_s)}s"
                break
            if wants_stream:
                # A long answer must be allowed to finish; only the wait for
                # response headers is bounded by the retry budget.
                call_timeout = aiohttp.ClientTimeout(
                    total=None, connect=min(10.0, max(1.0, remaining)),
                    sock_read=timeout_s)
            else:
                call_timeout = aiohttp.ClientTimeout(
                    total=max(1.0, remaining),
                    connect=min(10.0, max(1.0, remaining)),
                    sock_read=max(1.0, remaining))
            attempt_body = body
            if target and target != entry["model"]:
                replaced = _rewrite_model_body(body, entry["model"], target)
                if replaced is not None:
                    attempt_body = replaced
            entry["to"] = target or entry["model"]
            entry["attempts"] = attempt + 1
            stream = None  # set once response headers are on the wire
            try:
                async with _upstream_slot(request, cfg):
                    async with request.app[SESSION].request(
                            request.method, url, data=attempt_body or None,
                            headers=headers, timeout=call_timeout) as upstream:
                        stats["upstream_ok"] = True
                        call_ms = int((time.monotonic() - started) * 1000)
                        # A 5xx from a route means this route cannot serve the
                        # model (dead relay, no quota); try the next candidate
                        # before the phone ever sees a byte.
                        # 504 = 上游网关等首包超时（实测 jlcops 的 LB 是 60s 墙）。
                        # 它可能已经把请求交给模型了，换路由重试等于重复计费/重复
                        # 执行，所以 504 直接回给调用方；只有 502/503 才换下一条。
                        retryable = upstream.status in (502, 503)
                        if (retryable and target
                                and attempt + 1 < len(targets)):
                            last_status = upstream.status
                            # 直连路由的上游 5xx 多半是 provider 过载，别标死它
                            if _relay_prefix(target):
                                _route_mark(target, False, call_ms)
                            stats["last_error"] = (
                                f"route {target} HTTP {upstream.status}; "
                                "retrying another route")
                            continue
                        if target:
                            _route_mark(target, True, call_ms)
                        stats["last_error"] = (
                            "" if upstream.status < 400 else
                            f"upstream HTTP {upstream.status} on {request.path}")
                        ctype = upstream.headers.get("content-type",
                                                     "application/json")
                        out_headers = {h: upstream.headers[h] for h in PASS_HEADERS
                                       if h in upstream.headers}
                        out_headers.update(CORS_HEADERS)
                        if target:
                            # Which route actually answered, and whether a
                            # fallback happened: the phone-side debugging trail
                            # without reading gateway logs.
                            out_headers["x-relaydesk-route"] = target
                            if attempt:
                                out_headers["x-relaydesk-attempts"] = str(
                                    attempt + 1)
                        if "text/event-stream" in ctype:
                            entry["stream"] = True
                            entry["status"] = upstream.status
                            resp = web.StreamResponse(status=upstream.status,
                                                      headers=out_headers)
                            resp.headers.setdefault("Cache-Control", "no-cache")
                            resp.headers.setdefault("X-Accel-Buffering", "no")
                            await resp.prepare(request)
                            stream = resp  # from here: never return another Response
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
                        if (request.path.rstrip("/") == "/v1/models"
                                and upstream.status < 400):
                            # 缓存必须用**上游原始**目录：`_normalize_models_payload`
                            # 会把已知不通的中继路由过滤掉，若拿过滤后的结果写缓存，
                            # 缓存会一次次变小，那些模型就再也回不来了（实测 39→11）。
                            with contextlib.suppress(ValueError,
                                                     UnicodeDecodeError):
                                _store_catalog(cfg["upstream"].rstrip("/"),
                                               _parse_catalog(json.loads(payload)))
                            payload = _normalize_models_payload(payload)
                        # 上游可能"状态码 200 但内容不是 JSON"——常见于中间 LB
                        # 塞进来的一张 HTML 错误页（第 126 轮见过 `Server: volcalb`
                        # 的 HTML 504），也见过直接空体。原样转发会让客户端
                        # 收到「200 但没有内容」，比明确的错误更难排查。
                        # 只在非流式、且是补全类路径时校验；合法 JSON 不会误杀。
                        if (upstream.status < 400
                                and request.path.rstrip("/") in
                                ("/v1/messages", "/v1/chat/completions")):
                            body_ok = False
                            if payload:
                                with contextlib.suppress(ValueError,
                                                         UnicodeDecodeError):
                                    body_ok = isinstance(
                                        json.loads(payload), dict)
                            if not body_ok:
                                stats["failed"] += 1
                                stats["last_error"] = (
                                    f"upstream answered {upstream.status} with a "
                                    f"non-JSON body ({len(payload)} bytes)")
                                return fail(
                                    502, "api_error",
                                    "上游返回了 HTTP 200 但内容不是 JSON "
                                    f"（{len(payload)} 字节）——通常是中间的负载均衡"
                                    "或代理插了一张错误页。稍后重试；持续出现请检查 "
                                    "cc-switch 与它背后的服务")
                        out_headers.pop("content-type", None)
                        entry["status"] = upstream.status
                        # aiohttp rejects a charset inside `content_type`, so the
                        # media type (with UTF-8 declared) goes in as a header.
                        out_headers["Content-Type"] = _declared_ctype(ctype)
                        return web.Response(status=upstream.status, body=payload,
                                            headers=out_headers)
            except QueueFull:
                stats["rejected"] += 1
                stats["last_error"] = ("queue full while waiting for an "
                                       "upstream slot")
                return fail(429, "overloaded_error",
                            "上游并发已满且排队超时（cc-switch 只接受约 5 路并发），"
                            "请稍后重试", headers={"Retry-After": "5"})
            except asyncio.TimeoutError:
                last_error = f"upstream timed out after {int(timeout_s)}s"
                stats["last_error"] = (
                    f"route {target} timed out" if target else last_error)
                if target:
                    _route_mark(target, False,
                                int((time.monotonic() - started) * 1000))
                if stream is not None:
                    stats["aborted"] += 1
                    return stream
                if (target and attempt + 1 < len(targets)
                        and deadline - time.monotonic() > 1.0):
                    continue
                stats["failed"] += 1
                return fail(504, "timeout_error", last_error)
            except aiohttp.ClientError as exc:
                last_error = f"upstream unreachable: {type(exc).__name__}"
                stats["upstream_ok"] = False
                stats["last_error"] = last_error
                # cc-switch restarts (auto-update) close the listener for a
                # moment. A refused *connect* proves the request never ran, so
                # one same-route retry is safe and turns a random 502 into a
                # normal answer.
                if (stream is None
                        and isinstance(exc, aiohttp.ClientConnectorError)
                        and target not in connect_retried):
                    connect_retried.add(target)
                    stats["last_error"] = (f"route {target} refused "
                                           "connection; retrying once")
                    await asyncio.sleep(0.4)
                    targets.insert(attempt + 1, target)
                    continue
                if target:
                    _route_mark(target, False,
                                int((time.monotonic() - started) * 1000))
                if stream is not None:
                    # Headers already sent: writing a second response here
                    # would corrupt the chunked stream, so close and report by
                    # disconnect.
                    stats["aborted"] += 1
                    with contextlib.suppress(Exception):
                        await stream.write_eof()
                    return stream
                if target and attempt + 1 < len(targets):
                    continue
                # A phone that cannot read the catalog cannot pick a model
                # either; serving the last good copy beats a dead picker while
                # cc-switch restarts. The header says the data is stale.
                if request.path.rstrip("/") == "/v1/models":
                    cached = _cached_catalog(cfg["upstream"].rstrip("/"))
                    if cached and cached.get("ids"):
                        ids = cached["ids"]
                        body_out = json.dumps({
                            "object": "list",
                            "data": [
                                _model_entry(model_id,
                                             _display_with_health(model_id,
                                                                  cached))
                                for model_id in ids],
                            "first_id": ids[0], "last_id": ids[-1],
                            "has_more": False,
                        }, ensure_ascii=False).encode("utf-8")
                        stats["ok"] += 1
                        entry["status"] = 200
                        entry["cached"] = True
                        return web.Response(
                            status=200, body=body_out,
                            headers={**CORS_HEADERS,
                                     "x-relaydesk-cached": "1"},
                            content_type="application/json")
                stats["failed"] += 1
                _note_reject(stats, "502 upstream", request.remote)
                return fail(502, "api_error",
                            f"upstream {cfg['upstream']} unreachable: "
                            f"{type(exc).__name__}")
        # Every candidate route answered 5xx: report the last status, which is
        # the one the user's model actually hit.
        stats["failed"] += 1
        stats["last_error"] = (f"all {len(targets)} routes failed; last HTTP "
                               f"{last_status}")
        return fail(last_status if last_status >= 500 else 502, "api_error",
                    f"no route could serve {entry['model']} "
                    f"(last upstream HTTP {last_status})")
    finally:
        entry["ms"] = int((time.monotonic() - started) * 1000)
        _note_request(stats, entry)
        if reserved:
            _leave_active(stats, active_key)


# -- OpenAI-compatible front door -------------------------------------------
# Most phone LLM clients only speak the OpenAI protocol while cc-switch only
# exposes Anthropic Messages. The gateway translates both ways, so model
# mapping, routing and billing still happen in exactly one place.

def _oai_error(status: int, message: str,
               kind: str = "invalid_request_error",
               code: str | None = None,
               headers: dict | None = None) -> web.Response:
    merged = dict(CORS_HEADERS)
    if headers:
        merged.update(headers)
    return web.json_response(
        {"error": {"message": message, "type": kind, "code": code or kind}},
        status=status, headers=merged)


def _oai_blocks(content) -> list[dict]:
    """OpenAI message content (string or typed parts) -> Anthropic blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    blocks: list[dict] = []
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype in ("text", "input_text"):
                blocks.append({"type": "text",
                               "text": str(part.get("text") or "")})
            elif ptype == "image_url":
                url = str((part.get("image_url") or {}).get("url") or "")
                if url.startswith("data:"):
                    header, _, data = url.partition(",")
                    media = header[5:].split(";")[0] or "image/png"
                    blocks.append({"type": "image",
                                   "source": {"type": "base64",
                                              "media_type": media,
                                              "data": data}})
                elif url:
                    blocks.append({"type": "image",
                                   "source": {"type": "url", "url": url}})
    return blocks


def _tool_result_content(content) -> str:
    """OpenAI tool message content -> the string Anthropic tool_result wants."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") in ("text",
                                                                 "input_text"):
                parts.append(str(part.get("text") or ""))
        return "\n".join(parts)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def _oai_tools_to_anthropic(tools) -> list[dict]:
    """OpenAI ``tools`` -> Anthropic tool definitions (function only)."""
    out: list[dict] = []
    if not isinstance(tools, list):
        return out
    for item in tools:
        if not isinstance(item, dict):
            continue
        # Some clients send {"type": "function", "name": ...} without nesting.
        fn = item.get("function") if isinstance(item.get("function"), dict) else item
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        schema = fn.get("parameters")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        entry = {"name": name, "input_schema": schema}
        if fn.get("description"):
            entry["description"] = str(fn["description"])
        out.append(entry)
    return out


def _oai_tool_choice(choice) -> dict | None:
    """OpenAI ``tool_choice`` -> Anthropic ``tool_choice``."""
    if isinstance(choice, dict):
        fn = choice.get("function") if isinstance(choice.get("function"),
                                                  dict) else choice
        name = str(fn.get("name") or "").strip()
        return {"type": "tool", "name": name} if name else None
    if choice == "auto":
        return {"type": "auto"}
    if choice == "required":
        return {"type": "any"}
    return None  # "none" is handled by dropping the tools entirely


def oai_request_to_anthropic(payload: dict, model: str) -> dict:
    """Translate a chat-completions request; raises ValueError when unusable."""
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages 不能为空")
    system_parts: list[str] = []
    converted: list[dict] = []
    pending_results: list[dict] = []

    def flush_results() -> None:
        nonlocal pending_results
        if pending_results:
            # Consecutive tool messages become one user turn: Anthropic wants
            # tool_result blocks inside a user message, after the assistant.
            converted.append({"role": "user", "content": pending_results})
            pending_results = []

    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("messages 里有非法元素")
        role = message.get("role")
        if role in ("system", "developer"):
            content = message.get("content")
            if isinstance(content, str):
                system_parts.append(content)
            else:
                for block in _oai_blocks(content):
                    if block["type"] == "text":
                        system_parts.append(block["text"])
            continue
        if role == "tool":
            pending_results.append({
                "type": "tool_result",
                "tool_use_id": str(message.get("tool_call_id") or ""),
                "content": _tool_result_content(message.get("content")),
            })
            continue
        flush_results()
        if role not in ("user", "assistant"):
            raise ValueError(f"暂不支持 role={role}")
        blocks = _oai_blocks(message.get("content"))
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                name = str(fn.get("name") or "").strip()
                if not name:
                    continue
                arguments = fn.get("arguments")
                if isinstance(arguments, str):
                    try:
                        parsed = json.loads(arguments or "{}")
                    except ValueError:
                        parsed = {"_raw": arguments}
                elif isinstance(arguments, dict):
                    parsed = arguments
                else:
                    parsed = {}
                blocks.append({
                    "type": "tool_use",
                    "id": str(call.get("id")
                              or f"call_{secrets.token_hex(6)}"),
                    "name": name,
                    "input": parsed if isinstance(parsed, dict) else {
                        "_raw": parsed},
                })
        if blocks:
            converted.append({"role": role, "content": blocks})
    flush_results()
    if not converted:
        raise ValueError("messages 里没有可发送的用户内容")
    try:
        max_tokens = int(payload.get("max_tokens")
                         or payload.get("max_completion_tokens") or 4096)
    except (TypeError, ValueError):
        max_tokens = 4096
    body = {"model": model, "messages": converted,
            "max_tokens": max(1, min(64000, max_tokens))}
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    for key in ("temperature", "top_p"):
        if isinstance(payload.get(key), (int, float)):
            body[key] = payload[key]
    stop = payload.get("stop")
    if isinstance(stop, str) and stop:
        body["stop_sequences"] = [stop]
    elif isinstance(stop, list) and stop:
        body["stop_sequences"] = [str(item) for item in stop][:4]
    if payload.get("stream"):
        body["stream"] = True
    tools = _oai_tools_to_anthropic(payload.get("tools"))
    if payload.get("tool_choice") == "none":
        tools = []
    if tools:
        body["tools"] = tools
        mapped = _oai_tool_choice(payload.get("tool_choice"))
        if mapped:
            body["tool_choice"] = mapped
    return body


def oai_usage(usage: dict | None) -> dict:
    usage = usage or {}
    prompt = int(usage.get("input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion}


def _split_anthropic_content(data: dict) -> tuple[str, str]:
    """(answer text, reasoning text) out of an Anthropic content list.

    Reasoning models answer with ``thinking`` blocks first; OpenAI clients
    expect the answer in ``content`` and show nothing otherwise, so reasoning
    is exposed in the de-facto ``reasoning_content`` field instead of being
    dropped.
    """
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    for block in data.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(str(block.get("text") or ""))
        elif block.get("type") == "thinking":
            thinking_parts.append(str(block.get("thinking") or ""))
    return "".join(text_parts), "".join(thinking_parts)


def _anthropic_tool_calls(data: dict) -> list[dict]:
    """Anthropic tool_use blocks -> OpenAI ``tool_calls``."""
    calls: list[dict] = []
    for block in data.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        calls.append({"id": block.get("id") or f"call_{secrets.token_hex(6)}",
                      "type": "function",
                      "function": {
                          "name": str(block.get("name") or ""),
                          "arguments": json.dumps(block.get("input") or {},
                                                  ensure_ascii=False)}})
    return calls


def anthropic_to_oai_response(data: dict, model: str) -> dict:
    text, reasoning = _split_anthropic_content(data)
    calls = _anthropic_tool_calls(data)
    stop = FINISH_REASONS.get(str(data.get("stop_reason") or "end_turn"),
                              "stop")
    if calls:
        finish = "tool_calls"
    else:
        finish = stop
    # Some reasoning models spend a small max_tokens entirely on thinking; a
    # phone client that cannot render reasoning_content would show a blank
    # answer. Fall back to the reasoning text when there is no answer and no
    # tool call to return (a client that understands reasoning_content still
    # gets it).
    message: dict = {"role": "assistant",
                     "content": text or (None if calls else reasoning)}
    if reasoning:
        message["reasoning_content"] = reasoning
    if calls:
        message["tool_calls"] = calls
    return {"id": data.get("id") or f"chatcmpl-{secrets.token_hex(8)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": message,
                         "finish_reason": finish}],
            "usage": oai_usage(data.get("usage"))}


def oai_error_from_upstream(payload: bytes) -> tuple[str, str]:
    """(message, type) out of an Anthropic error body, for OpenAI clients."""
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return payload.decode("utf-8", "replace")[:300], "api_error"
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return (str(error.get("message") or ""),
                str(error.get("type") or "api_error"))
    return str(data)[:300], "api_error"


def _oai_chunk(chunk_id: str, model: str, created: int,
               delta: dict | None = None, finish: str | None = None,
               usage: dict | None = None) -> dict:
    body = {"id": chunk_id, "object": "chat.completion.chunk",
            "created": created, "model": model,
            "choices": [] if usage else [{"index": 0,
                                          "delta": delta or {},
                                          "finish_reason": finish}]}
    if usage:
        body["usage"] = usage
    return body


async def _sse_payloads(upstream):
    """Yield decoded JSON payloads out of an Anthropic SSE response."""
    buffer = b""
    async for chunk in upstream.content.iter_any():
        buffer = (buffer + chunk).replace(b"\r\n", b"\n")
        while b"\n\n" in buffer:
            raw, buffer = buffer.split(b"\n\n", 1)
            for line in raw.split(b"\n"):
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    yield json.loads(payload)
                except (ValueError, UnicodeDecodeError):
                    continue


async def _oai_prepare(request: web.Request, target: str) -> web.StreamResponse:
    resp = web.StreamResponse(headers={
        **CORS_HEADERS,
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "x-relaydesk-route": target,
    })
    await resp.prepare(request)
    return resp


async def _oai_write(resp: web.StreamResponse, obj: dict) -> None:
    await resp.write(b"data: " + json.dumps(obj, ensure_ascii=False)
                     .encode("utf-8") + b"\n\n")


async def _pipe_oai_stream(request: web.Request, upstream, target: str,
                           requested: str, stats: dict, entry: dict,
                           include_usage: bool = False) -> web.StreamResponse:
    """Anthropic SSE -> OpenAI chat.completion.chunk events."""
    resp = await _oai_prepare(request, target)
    chunk_id = f"chatcmpl-{secrets.token_hex(8)}"
    created = int(time.time())
    model = requested
    usage: dict | None = None
    finish: str | None = None
    started = False
    # Anthropic streams each tool call as its own content block; OpenAI wants a
    # stable numeric index per call inside delta.tool_calls.
    tool_index_by_block: dict[int, int] = {}
    next_tool_index = 0
    text_sent = 0
    reasoning_text: list[str] = []
    try:
        async for event in _sse_payloads(upstream):
            etype = event.get("type")
            if etype == "message_start":
                message = event.get("message") or {}
                model = message.get("model") or model
                usage = message.get("usage") or usage
                if not started:
                    started = True
                    await _oai_write(resp, _oai_chunk(
                        chunk_id, model, created, {"role": "assistant"}))
            elif etype == "content_block_start":
                block = event.get("content_block") or {}
                if block.get("type") == "tool_use":
                    block_index = int(event.get("index") or 0)
                    tool_index_by_block[block_index] = next_tool_index
                    await _oai_write(resp, _oai_chunk(
                        chunk_id, model, created,
                        {"tool_calls": [{
                            "index": next_tool_index,
                            "id": block.get("id") or "",
                            "type": "function",
                            "function": {"name": block.get("name") or "",
                                         "arguments": ""}}]}))
                    next_tool_index += 1
            elif etype == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    text_sent += len(delta["text"])
                    await _oai_write(resp, _oai_chunk(
                        chunk_id, model, created,
                        {"content": delta["text"]}))
                elif (delta.get("type") == "thinking_delta"
                      and delta.get("thinking")):
                    # Reasoning models emit thinking first; keep it visible to
                    # clients that render reasoning_content, and do not let it
                    # starve the answer content.
                    reasoning_text.append(delta["thinking"])
                    await _oai_write(resp, _oai_chunk(
                        chunk_id, model, created,
                        {"reasoning_content": delta["thinking"]}))
                elif delta.get("type") == "input_json_delta":
                    block_index = int(event.get("index") or 0)
                    tool_index = tool_index_by_block.get(block_index, 0)
                    await _oai_write(resp, _oai_chunk(
                        chunk_id, model, created,
                        {"tool_calls": [{
                            "index": tool_index,
                            "function": {"arguments":
                                         delta.get("partial_json") or ""}}]}))
            elif etype == "message_delta":
                stop = (event.get("delta") or {}).get("stop_reason")
                if str(stop) == "tool_use" or next_tool_index:
                    finish = "tool_calls"
                else:
                    finish = FINISH_REASONS.get(str(stop or "end_turn"), "stop")
                usage = event.get("usage") or usage
            elif etype == "message_stop":
                break
        # A reasoning model can spend a small max_tokens entirely on thinking.
        # A phone client that cannot render reasoning_content would show a
        # blank stream, so replay the reasoning as the answer in that case
        # (matching what the non-streaming path already does).
        if not text_sent and not next_tool_index and reasoning_text:
            await _oai_write(resp, _oai_chunk(
                chunk_id, model, created,
                {"content": "".join(reasoning_text)}))
        await _oai_write(resp, _oai_chunk(chunk_id, model, created, {},
                                          finish or "stop"))
        if include_usage and usage:
            await _oai_write(resp, _oai_chunk(chunk_id, model, created,
                                              usage=oai_usage(usage)))
        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        stats["ok"] += 1
    except (ConnectionResetError, BrokenPipeError, aiohttp.ClientError):
        stats["aborted"] += 1
        with contextlib.suppress(Exception):
            await resp.write_eof()
    entry["status"] = 200
    return resp


async def _oai_one_shot_stream(request: web.Request, data: dict,
                               target: str, requested: str,
                               stats: dict, entry: dict,
                               include_usage: bool) -> web.StreamResponse:
    """Upstream answered JSON to a stream request: replay it as one chunk."""
    resp = await _oai_prepare(request, target)
    chunk_id = f"chatcmpl-{secrets.token_hex(8)}"
    created = int(time.time())
    payload = anthropic_to_oai_response(data, data.get("model") or requested)
    await _oai_write(resp, _oai_chunk(chunk_id, payload["model"], created,
                                      {"role": "assistant"}))
    reasoning = (payload["choices"][0]["message"].get("reasoning_content")
                 or "")
    if reasoning:
        await _oai_write(resp, _oai_chunk(chunk_id, payload["model"], created,
                                          {"reasoning_content": reasoning}))
    text = payload["choices"][0]["message"]["content"]
    if text:
        await _oai_write(resp, _oai_chunk(chunk_id, payload["model"], created,
                                          {"content": text}))
    calls = payload["choices"][0]["message"].get("tool_calls") or []
    for index, call in enumerate(calls):
        await _oai_write(resp, _oai_chunk(
            chunk_id, payload["model"], created,
            {"tool_calls": [{"index": index, **call}]}))
    await _oai_write(resp, _oai_chunk(chunk_id, payload["model"], created, {},
                                      payload["choices"][0]["finish_reason"]))
    if include_usage and payload.get("usage"):
        await _oai_write(resp, _oai_chunk(chunk_id, payload["model"], created,
                                          usage=payload["usage"]))
    await resp.write(b"data: [DONE]\n\n")
    await resp.write_eof()
    stats["ok"] += 1
    entry["status"] = 200
    return resp


async def _openai_chat(request: web.Request) -> web.StreamResponse:
    """POST /v1/chat/completions, translated onto cc-switch's Anthropic route."""
    cfg = request.app[CFG]
    stats = request.app[STATS]
    started = time.monotonic()
    entry = {"t": int(time.time()), "path": request.path, "model": "",
             "to": "", "remote": request.remote or "", "stream": False,
             "status": 0, "ms": 0, "protocol": "openai"}
    reserved = False

    def fail(status: int, message: str, kind: str = "invalid_request_error",
             code: str | None = None,
             headers: dict | None = None) -> web.Response:
        entry["status"] = status
        entry["error"] = kind
        return _oai_error(status, message, kind, code, headers)

    try:
        blocked = _auth_block_remaining(stats, request.remote or "")
        if blocked:
            stats["rejected"] += 1
            _note_reject(stats, "429 auth-flood", request.remote)
            return fail(429, f"too many invalid tokens from this address; "
                             f"retry in {blocked}s", "rate_limit_error",
                        headers={"Retry-After": str(blocked)})
        if not _authorized(request, cfg["token"]):
            stats["rejected"] += 1
            _auth_note_fail(stats, request.remote or "")
            _note_reject(stats, _reject_kind(request, stats, request.remote),
                         request.remote)
            return fail(401, AUTH_HINT, "authentication_error")
        _auth_clear(stats, request.remote or "")
        limit = int(cfg.get("max_concurrent") or 0)
        if limit > 0 and stats["active"] >= limit:
            stats["rejected"] += 1
            _note_reject(stats, "429 busy", request.remote)
            return fail(429, "gateway busy; retry shortly", "rate_limit_error")
        active_key = _enter_active(stats, request)
        reserved = True
        try:
            body = await _read_body(request, cfg)
        except BodyTooLarge:
            stats["failed"] += 1
            return fail(413, "请求体超过 max_body_mb", "invalid_request_error")
        except BodyTimeout:
            stats["failed"] += 1
            return fail(408, "请求体传输停滞（手机端没有继续发送）",
                        "api_error")
        try:
            payload = json.loads(body) if body else None
        except (ValueError, UnicodeDecodeError):
            payload = None
        if not isinstance(payload, dict):
            stats["failed"] += 1
            return fail(400, "body 必须是 JSON 对象")
        requested = str(payload.get("model") or "").strip()
        if not requested:
            stats["failed"] += 1
            return fail(400, "缺少 model")
        entry["model"] = requested
        wants_stream = bool(payload.get("stream"))
        entry["stream"] = wants_stream
        include_usage = bool((payload.get("stream_options") or {})
                             .get("include_usage"))
        headers = {"content-type": "application/json",
                   "anthropic-version": DEFAULT_ANTHROPIC_VERSION}
        if request.headers.get("anthropic-beta"):
            headers["anthropic-beta"] = request.headers["anthropic-beta"]
        if cfg.get("upstream_token"):
            headers["x-api-key"] = cfg["upstream_token"]
        stats["requests"] += 1
        stats["last"] = int(time.time())
        catalog = await catalog_async(cfg, request.app[SESSION])
        if catalog.get("ids") and not catalog_matches(requested, catalog):
            # 可能是刚新增的模型：强制重取一次再判（同上）
            fresh = await catalog_async(cfg, request.app[SESSION], force=True)
            if fresh.get("ids"):
                catalog = fresh
        if catalog.get("ids") and not catalog_matches(requested, catalog):
            stats["failed"] += 1
            stats["last_error"] = f"unknown model {requested}"
            return fail(400, f"未知模型 {requested}：cc-switch 目录里没有。"
                             f"可用模型：{model_hint(catalog)}"
                             "（GET /v1/models 或 flix models list 看全部）",
                        "invalid_request_error", "model_not_found")
        targets = (resolve_candidates(requested, catalog)
                   or [requested])[:MAX_ROUTE_ATTEMPTS]
        if any(_relay_prefix(target) for target in targets):
            targets = await _usable_targets(cfg, request.app[SESSION], targets)
        if targets and all(_route_dead_recently(x) for x in targets):
            targets = []
        if not targets:
            stats["failed"] += 1
            stats["last_error"] = (f"all routes for {requested} known "
                                   "unreachable")
            return fail(503, f"没有可用路由：{requested} 的候选路由近期都失败过，"
                             "等中继恢复后自动重试（10 分钟窗口）", "api_error")
        timeout_s = float(cfg.get("upstream_timeout_s")
                          or DEFAULT_UPSTREAM_TIMEOUT_S)
        deadline = time.monotonic() + timeout_s
        last_status = 0
        last_message = ""
        connect_retried: set[str] = set()
        for attempt, target in enumerate(targets):
            remaining = deadline - time.monotonic()
            if attempt and remaining <= 1.0:
                break
            entry["to"] = target
            entry["attempts"] = attempt + 1
            try:
                anth_body = oai_request_to_anthropic(payload, target)
            except ValueError as exc:
                stats["failed"] += 1
                return fail(400, str(exc))
            if wants_stream:
                call_timeout = aiohttp.ClientTimeout(
                    total=None, connect=min(10.0, max(1.0, remaining)),
                    sock_read=timeout_s)
            else:
                call_timeout = aiohttp.ClientTimeout(
                    total=max(1.0, remaining),
                    connect=min(10.0, max(1.0, remaining)),
                    sock_read=max(1.0, remaining))
            try:
                async with _upstream_slot(request, cfg):
                    async with request.app[SESSION].post(
                            cfg["upstream"] + "/v1/messages",
                            json=anth_body, headers=headers,
                            timeout=call_timeout) as upstream:
                        stats["upstream_ok"] = True
                        ctype = upstream.headers.get("content-type",
                                                     "application/json")
                        if upstream.status >= 400:
                            err_payload = await upstream.read()
                            last_message, _kind = oai_error_from_upstream(
                                err_payload)
                            last_status = upstream.status
                            # 只在中继上把 5xx 记为路由死亡；直连交给退避
                            _route_mark(target, upstream.status < 500
                                        or not _relay_prefix(target))
                            if upstream.status >= 500 and attempt + 1 < len(targets):
                                stats["last_error"] = (f"route {target} HTTP "
                                                       f"{upstream.status}; retrying")
                                continue
                            stats["failed"] += 1
                            stats["last_error"] = (f"upstream HTTP {upstream.status}"
                                                   f" on {request.path}")
                            kind = {401: "authentication_error",
                                    429: "rate_limit_error"}.get(upstream.status,
                                                                 "api_error")
                            return fail(upstream.status,
                                        last_message or "upstream error", kind,
                                        str(upstream.status))
                        _route_mark(target, True)
                        stats["last_error"] = ""
                        if wants_stream and "text/event-stream" in ctype:
                            return await _pipe_oai_stream(
                                request, upstream, target, requested, stats, entry,
                                include_usage)
                        data = await upstream.json(content_type=None)
                        if not isinstance(data, dict):
                            data = {}
                        if wants_stream:
                            return await _oai_one_shot_stream(
                                request, data, target, requested, stats, entry,
                                include_usage)
                        stats["ok"] += 1
                        stats["last_error"] = ""
                        payload_out = anthropic_to_oai_response(
                            data, str(data.get("model") or target))
                        resp_headers = {**CORS_HEADERS,
                                        "x-relaydesk-route": target}
                        if attempt:
                            resp_headers["x-relaydesk-attempts"] = str(attempt + 1)
                        # 记账：不写这一行 recent_requests 里就是 status=0，
                        # 看诊时误以为请求没走完（实测踩过）。
                        entry["status"] = 200
                        _route_mark(target, True)
                        return web.json_response(payload_out,
                                                 headers=resp_headers)
            except QueueFull:
                stats["rejected"] += 1
                stats["last_error"] = "queue full waiting for upstream slot"
                return fail(429, "上游并发已满且排队超时，请稍后重试",
                            "rate_limit_error", headers={"Retry-After": "5"})
            except asyncio.TimeoutError:
                last_message = f"upstream timed out after {int(timeout_s)}s"
                stats["last_error"] = last_message
                _route_mark(target, False)
                if attempt + 1 < len(targets) and deadline - time.monotonic() > 1.0:
                    continue
                stats["failed"] += 1
                return fail(504, last_message, "api_error")
            except aiohttp.ClientError as exc:
                stats["upstream_ok"] = False
                last_message = f"upstream unreachable: {type(exc).__name__}"
                stats["last_error"] = last_message
                # Same reasoning as the Anthropic path: a refused connect means
                # the request never reached cc-switch, so retry the same route
                # once (covers the auto-update restart window).
                if (isinstance(exc, aiohttp.ClientConnectorError)
                        and target not in connect_retried):
                    connect_retried.add(target)
                    stats["last_error"] = (f"route {target} refused "
                                           "connection; retrying once")
                    await asyncio.sleep(0.4)
                    targets.insert(attempt + 1, target)
                    continue
                _route_mark(target, False)
                if attempt + 1 < len(targets):
                    continue
                stats["failed"] += 1
                return fail(502, last_message, "api_error")
        stats["failed"] += 1
        stats["last_error"] = (last_message
                               or f"all {len(targets)} routes failed; "
                                  f"last HTTP {last_status}")
        return fail(last_status if last_status >= 500 else 502,
                    last_message or "no route could serve the model",
                    "api_error")
    finally:
        entry["ms"] = int((time.monotonic() - started) * 1000)
        _note_request(stats, entry)
        if reserved:
            _leave_active(stats, active_key)


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
        return web.json_response({"ok": True}, headers=dict(CORS_HEADERS))
    stats["uptime"] = max(0, int(time.time()) - stats.pop("started", 0))
    # 自愈：超过 2 倍总预算还没收尾的登记项视为陈旧，避免 active 永远卡住
    _sweep_active(stats, max(60.0, 2 * float(cfg.get("upstream_timeout_s") or 180)))
    catalog = _cached_catalog(cfg["upstream"].rstrip("/")) or {}
    health = route_health()
    # 对外口径：models 是客户端真正能选的（已过滤死路由），
    # catalog_models 是上游目录原始条数，便于诊断「为什么少了几个」。
    visible = (len(visible_model_ids(
        catalog, pending_relays_hidden=not _WARM_STATE["done_at"]))
        if catalog else 0)
    raw_count = len(catalog.get("ids") or [])
    resp = web.json_response(
        {"ok": True, "upstream": cfg["upstream"], "port": cfg["port"],
         # What the phone picker last saw, and whether the last proxied call
         # actually reached the upstream: one glance explains "手机连不上".
         "models": visible,
         "catalog_models": raw_count,
         "upstream_ok": stats.get("upstream_ok"),
         "last_error": stats.get("last_error", ""),
         "upstream_timeout_s": cfg.get("upstream_timeout_s"),
         "routes": health,
         "relay_prefixes": relay_prefix_health(),
         "stats": stats}, headers=dict(CORS_HEADERS))
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def _preflight(request: web.Request) -> web.Response:
    """CORS preflight: a phone browser will not POST a custom header without it."""
    return web.Response(status=204, headers=dict(CORS_HEADERS))


PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RelayDesk 模型共享</title>
<style>
:root{color-scheme:dark}
body{font-family:system-ui,-apple-system,"PingFang SC",sans-serif;background:#0f1218;
color:#e8ecf2;margin:0;padding:18px 14px 40px;line-height:1.6;max-width:640px}
h2{font-size:19px;margin:4px 0 14px}
h3{font-size:15px;margin:0 0 8px;color:#9fb0c8}
.card{border:1px solid #26303f;background:#151b24;border-radius:12px;
padding:14px;margin:12px 0}
.row{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:6px 0}
.k{color:#8b9bb4;min-width:68px;font-size:13px}
code{background:#0b0f14;border:1px solid #26303f;padding:3px 7px;border-radius:6px;
word-break:break-all;font-size:13px}
button{padding:8px 14px;border:1px solid #2f6fd0;border-radius:8px;background:#1b63c8;
color:#fff;font-size:14px}
button.ghost{background:transparent;border-color:#33415a;color:#c6d3e6;padding:6px 10px;
font-size:12px}
#send{width:100%;margin-top:10px;padding:11px;font-size:15px}
select,textarea,input{width:100%;box-sizing:border-box;margin-top:6px;padding:10px;
border-radius:8px;border:1px solid #2b374a;background:#0b0f14;color:#e8ecf2;
font-size:14px;font-family:inherit}
textarea{resize:vertical}
label{display:flex;align-items:center;gap:6px;margin-top:8px;font-size:13px;
color:#a9b8cd}
label input{width:auto;margin:0}
.result{margin-top:10px;font-size:13px;white-space:pre-wrap;word-break:break-word;
min-height:20px}
.meta{color:#8b9bb4;font-size:12px}
.ok{color:#4ade80}.bad{color:#f87171}
.hint{color:#7c8ba1;font-size:12px;margin:8px 0 0}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:6px 14px;font-size:13px}
</style></head><body>
<h2>RelayDesk 模型共享</h2>

<div class="card">
  <div class="row"><span class="k">协议</span><span>Anthropic Messages</span></div>
  <div class="row"><span class="k">Base URL</span><code id="base">{{BASE}}</code>
    <button class="ghost" data-copy="base">复制</button></div>
  <div class="row"><span class="k">API Key</span><code id="tok">{{TOKEN}}</code>
    <button class="ghost" data-copy="tok">复制</button></div>
  <div class="row"><span class="k">上游</span><span class="meta">{{UPSTREAM}}</span></div>
  <p class="hint">手机 App 里协议选 <b>Anthropic</b>，填入上面的地址和 Key。
  只支持 OpenAI 协议的 App（大多数）把协议选 <b>OpenAI</b>，Base URL 填同一地址
  （客户端会自动拼 <code>/v1/chat/completions</code>）。模型列表见下方自检框
  （value 就是模型 id）。</p>
  <p class="hint">{{OTHER_URLS}}</p>
</div>

<div class="card">
  <h3>连通性自检（在这台手机上真跑一次）</h3>
  <select id="model">{{MODEL_OPTIONS}}</select>
  <input id="modelManual" placeholder="或手填模型 id（可留空）">
  <textarea id="prompt" rows="2">用一句话介绍你自己</textarea>
  <label><input type="checkbox" id="openai"> 用 OpenAI 协议（/v1/chat/completions）</label>
  <label><input type="checkbox" id="stream" checked> 流式显示（SSE）</label>
  <button id="send">发送测试</button>
  <div id="result" class="result"></div>
  <button id="tooltest" class="ghost" style="width:100%;margin-top:10px;padding:9px">
    演示工具调用（两轮：模型调用工具 → 回传结果 → 最终回答）</button>
  <div id="toolresult" class="result"></div>
</div>

<div class="card">
  <h3>网关状态</h3>
  <div id="stats" class="meta">加载中…</div>
</div>
<p class="hint">本页只用于配置与自检，不代理其它接口；令牌泄漏后可在桌面端「重置令牌」。</p>

<script>
(function () {
  var BASE = {{BASE_JSON}};
  var TOKEN = {{TOKEN_JSON}};
  var out = document.getElementById('result');
  var statsEl = document.getElementById('stats');

  function esc(t) { return String(t == null ? '' : t); }
  function show(html) { out.innerHTML = html; }
  function fmtMs(ms) { return ms < 1000 ? ms + ' ms' : (ms / 1000).toFixed(1) + ' s'; }

  document.querySelectorAll('[data-copy]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var el = document.getElementById(btn.getAttribute('data-copy'));
      var text = el.textContent;
      // 只有真成功才说「已复制」。实测安卓 http 页面里
      // navigator.clipboard 不可用、execCommand('copy') 返回 false，
      // 而旧代码把返回值丢了、无条件显示「已复制」——按钮在说谎。
      var report = function (ok) {
        btn.textContent = ok ? '已复制' : '请长按手动复制';
        setTimeout(function () { btn.textContent = '复制'; },
                   ok ? 1500 : 2500);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(
          function () { report(true); },
          function () { report(false); });
      } else {
        var ta = document.createElement('textarea');
        ta.value = text; document.body.appendChild(ta); ta.select();
        var ok = false;
        try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
        document.body.removeChild(ta); report(ok);
      }
    });
  });

  function currentModel() {
    var manual = document.getElementById('modelManual').value.trim();
    if (manual) return manual;
    var sel = document.getElementById('model');
    return sel && sel.value ? sel.value : '';
  }

  function health() {
    fetch('/health', { headers: { 'x-api-key': TOKEN } })
      .then(function (r) { return r.json(); })
      .then(function (b) {
        var s = b.stats || {};
        var up = b.upstream_ok === false ? '<span class="bad">上游不可达</span>'
          : (b.upstream_ok === true ? '<span class="ok">上游正常</span>' : '上游未探测');
        var prefixes = Object.keys(b.relay_prefixes || {});
        statsEl.innerHTML =
          '<div class="grid"><div>网关 ' + up + '</div>' +
          '<div>模型 ' + esc(b.models || 0) + ' 个</div>' +
          '<div>请求 ' + esc(s.requests || 0) + '</div>' +
          '<div>成功 ' + esc(s.ok || 0) + ' / 失败 ' + esc(s.failed || 0) + '</div>' +
          '<div>进行中 ' + esc(s.active || 0) + '</div>' +
          '<div>已运行 ' + Math.floor((s.uptime || 0) / 60) + ' 分钟</div></div>' +
          (prefixes.length
            ? '<div class="bad" style="margin-top:6px">中继失败前缀 ' +
              prefixes.length + ' 个（对端 VPN/服务可能未连，这些路由暂不尝试）</div>'
            : '') +
          (b.last_error ? '<div class="bad" style="margin-top:6px">最近错误：' +
            esc(b.last_error) + '</div>' : '');
      })
      .catch(function (e) { statsEl.textContent = '状态读取失败：' + e; });
  }

  function renderText(text) {
    show('<div class="meta" id="metaLine"></div><div id="answer"></div>');
    document.getElementById('answer').textContent = text;
  }

  document.getElementById('send').addEventListener('click', function () {
    var model = currentModel();
    var prompt = document.getElementById('prompt').value || 'ping';
    if (!model) { show('<span class="bad">先选一个模型，或手填模型 id</span>'); return; }
    var btn = this; btn.disabled = true; btn.textContent = '请求中…';
    var streamBox = document.getElementById('stream');
    var useOpenai = document.getElementById('openai').checked;
    var proto = useOpenai ? 'OpenAI' : 'Anthropic';
    var endpoint = useOpenai ? '/v1/chat/completions' : '/v1/messages';
    var body = {
      model: model,
      max_tokens: 256,
      messages: [{ role: 'user', content: prompt }]
    };
    if (streamBox.checked) body.stream = true;
    var headers = { 'content-type': 'application/json' };
    if (useOpenai) {
      headers['authorization'] = 'Bearer ' + TOKEN;
    } else {
      headers['x-api-key'] = TOKEN;
      headers['anthropic-version'] = '2023-06-01';
    }
    var started = Date.now();
    fetch(endpoint, {
      method: 'POST',
      headers: headers,
      body: JSON.stringify(body)
    }).then(function (resp) {
      var ms = Date.now() - started;
      if (!resp.ok) {
        return resp.text().then(function (t) {
          show('<span class="bad">HTTP ' + resp.status + '</span> · ' +
               fmtMs(ms) + '<br><code>' + esc(t.slice(0, 800)) + '</code>');
        });
      }
      if (!body.stream) {
        return resp.json().then(function (j) {
          var text = '', used = 0, usedModel = model;
          if (useOpenai) {
            var choice = (j.choices || [])[0] || {};
            var msg = choice.message || {};
            text = msg.content || '';
            if (!text && msg.reasoning_content) {
              text = '（本模型只回了思考内容，未产生正文）\\n\\n' +
                     msg.reasoning_content.slice(0, 800);
            }
            used = (j.usage && j.usage.completion_tokens) || 0;
            usedModel = j.model || model;
          } else {
            text = (j.content || []).filter(function (c) {
              return c.type === 'text';
            }).map(function (c) { return c.text; }).join('');
            used = (j.usage && j.usage.output_tokens) || 0;
            usedModel = j.model || model;
          }
          show('<span class="ok">HTTP 200 · ' + proto + '</span> · ' +
               fmtMs(ms) + ' · 模型 <b>' + esc(usedModel) + '</b> · ' +
               used + ' tokens<br><br>' + esc(text || '（空回复）'));
        });
      }
      // SSE: read the stream and show deltas as they land.
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buf = '', text = '', reasoning = '', usedModel = model;
      show('<span class="meta">流式接收中（' + proto + '）…</span>');
      function pump() {
        return reader.read().then(function (chunk) {
          if (chunk.done) {
            var shown = text;
            if (!shown && reasoning) {
              shown = '（本模型只回了思考内容，未产生正文）\\n\\n' +
                      reasoning.slice(0, 800);
            }
            show('<span class="ok">HTTP 200 · ' + proto + '</span> · ' +
                 fmtMs(Date.now() - started) + ' · 模型 <b>' + esc(usedModel) +
                 '</b>（流式）<br><br>' + esc(shown || '（空回复）'));
            return;
          }
          buf += decoder.decode(chunk.value, { stream: true });
          var lines = buf.split('\\n');
          buf = lines.pop();
          lines.forEach(function (line) {
            if (line.indexOf('data:') !== 0) return;
            var raw = line.slice(5).trim();
            if (!raw || raw === '[DONE]') return;
            try {
              var ev = JSON.parse(raw);
              if (useOpenai) {
                if (ev.model) usedModel = ev.model;
                var choice = (ev.choices || [])[0];
                var delta = (choice && choice.delta) || {};
                if (delta.reasoning_content) {
                  reasoning += delta.reasoning_content;
                  show('<div class="meta">思考中 · ' +
                       fmtMs(Date.now() - started) + ' · ' +
                       reasoning.length + ' 字</div><div>' +
                       esc(text) + '</div>');
                }
                if (delta.content) {
                  text += delta.content;
                  show('<div class="meta">流式接收中 · ' +
                       fmtMs(Date.now() - started) + '</div><div>' +
                       esc(text) + '</div>');
                }
              } else if (ev.type === 'message_start' && ev.message) {
                usedModel = ev.message.model || usedModel;
              } else if (ev.type === 'content_block_delta' &&
                         ev.delta && ev.delta.type === 'text_delta') {
                text += ev.delta.text || '';
                show('<div class="meta">流式接收中 · ' +
                     fmtMs(Date.now() - started) + '</div><div>' +
                     esc(text) + '</div>');
              }
            } catch (e) {}
          });
          return pump();
        });
      }
      return pump();
    }).catch(function (e) {
      show('<span class="bad">请求失败：' + esc(e) + '</span>');
    }).then(function () {
      btn.disabled = false; btn.textContent = '发送测试'; health();
    });
  });

  // Tool-call demo: two real round trips through the gateway, from the phone.
  // Newlines are joined with String.fromCharCode(10): writing a backslash-n
  // here would be expanded by the Python template and break the whole script.
  var NL = String.fromCharCode(10);
  document.getElementById('tooltest').addEventListener('click', async function () {
    var btn = this;
    var out = document.getElementById('toolresult');
    var model = currentModel();
    if (!model) { out.textContent = '先选一个模型（需支持 function calling）'; return; }
    var label = btn.textContent;
    btn.disabled = true; btn.textContent = '执行中…';
    var tools = [{type: 'function', function: {name: 'get_time',
      description: '查询某个城市的当前时间',
      parameters: {type: 'object', properties: {city: {type: 'string'}},
                   required: ['city']}}}];
    var base = {model: model, max_tokens: 300, tools: tools,
                tool_choice: 'auto'};
    var headers = {'content-type': 'application/json',
                   'authorization': 'Bearer ' + TOKEN};
    var prompt = '现在北京几点？请调用 get_time 工具，city 传 Beijing。';
    try {
      out.textContent = '1) 请求模型调用 get_time…';
      var r1 = await fetch('/v1/chat/completions', {method: 'POST', headers: headers,
        body: JSON.stringify(Object.assign({}, base,
          {messages: [{role: 'user', content: prompt}]}))});
      var j1 = await r1.json();
      var msg = ((j1.choices || [])[0] || {}).message || {};
      var calls = msg.tool_calls || [];
      if (!calls.length) {
        out.textContent = ['模型没有发起工具调用（该模型可能不支持）。',
                           '返回内容：',
                           msg.content || JSON.stringify(j1).slice(0, 300)].join(NL);
        return;
      }
      var call = calls[0];
      var now = new Date().toLocaleString();
      out.textContent = ['1) 模型发起 ' + call.function.name + '(' +
                         call.function.arguments + ')',
                         '2) 回传工具结果…'].join(NL);
      var r2 = await fetch('/v1/chat/completions', {method: 'POST', headers: headers,
        body: JSON.stringify(Object.assign({}, base, {messages: [
          {role: 'user', content: prompt},
          {role: 'assistant', content: null, tool_calls: [call]},
          {role: 'tool', tool_call_id: call.id,
           content: now + '（Asia/Shanghai）'}
        ]}))});
      var j2 = await r2.json();
      var final = (((j2.choices || [])[0] || {}).message || {}).content || '（空）';
      out.innerHTML = '<span class="ok">工具调用通过</span>' + NL +
        esc('1) ' + call.function.name + '(' + call.function.arguments + ')') +
        NL + esc('2) 回传：' + now) + NL + esc('3) 模型最终回答：' + NL + final);
    } catch (e) {
      out.textContent = '失败：' + e;
    } finally {
      btn.disabled = false; btn.textContent = label;
    }
  });

  var count = document.getElementById('model');
  if (count && !count.options.length) {
    count.innerHTML = '<option value="">（上游未返回模型列表，请手填）</option>';
  }
  health();
})();
</script>
</body></html>"""


def _model_options(catalog: dict) -> str:
    """Phone picker entries: reachable routes first, dead ones marked."""
    rows = []
    for model_id in visible_model_ids(
            catalog, pending_relays_hidden=not _WARM_STATE["done_at"]):
        display = _display_with_health(model_id, catalog)
        rows.append(
            f'<option value="{html.escape(model_id)}">'
            f'{html.escape(display)}</option>')
    return "".join(rows)


def _annotate_urls(rows: list[dict]) -> str:
    """`url（interface）` joined for hints; bare URL when no name is known."""
    parts = []
    for row in rows:
        iface = row.get("interface") or ""
        parts.append(f"{row['url']}（{iface}）" if iface else row["url"])
    return "、".join(parts)


def _phone_base_url(request: web.Request, port: int) -> tuple[str, list[dict]]:
    """The address the phone can actually reach, plus any other candidates.

    `lan_urls()` lists the primary interface first, which is wrong on a
    tethered phone: it called in on 192.168.137.1 while the page advertised
    the Wi-Fi address. The request's own Host header is attacker-controlled,
    but it is only used when it names an IPv4 this machine really holds.
    """
    host = (request.headers.get("host") or "").strip()
    candidates = lan_urls_named(port)
    ours: set[str] = set()
    with contextlib.suppress(Exception):
        from . import local_ipv4s
        ours.update(local_ipv4s())
        ours.update(_adapter_ipv4s())
    parts = urlsplit("//" + host) if host else None
    hostname = (parts.hostname if parts else "") or ""
    if hostname in ("127.0.0.1", "localhost") or hostname in ours:
        base = f"{request.scheme}://{host}"
        others = [row for row in candidates if row["url"] != base]
        return base, others
    return (candidates[0]["url"], candidates[1:]) if candidates else ("", [])


async def _index(request: web.Request) -> web.Response:
    cfg = request.app[CFG]
    if not _authorized(request, cfg["token"], allow_query=True):
        return web.Response(status=401, text="需要令牌：/?token=<令牌>",
                            content_type="text/plain",
                            headers=dict(CORS_HEADERS))
    # The Host header is only echoed when it is one of our own addresses, so a
    # poisoned Host cannot put a hostile URL in front of the user.
    base, others = _phone_base_url(request, cfg["port"]) if cfg.get("port") \
        else ("", [])
    catalog = await catalog_async(cfg, request.app[SESSION], timeout=2.0)
    other_text = ("其它网卡地址：" + _annotate_urls(others)) if others else ""
    page = (PAGE
            .replace("{{BASE_JSON}}", json.dumps(base))
            .replace("{{TOKEN_JSON}}", json.dumps(cfg["token"]))
            .replace("{{MODELS_JSON}}", "[]")  # 模板已不使用 MODELS
            .replace("{{MODEL_OPTIONS}}", _model_options(catalog))
            .replace("{{OTHER_URLS}}", html.escape(other_text))
            .replace("{{BASE}}", html.escape(base))
            .replace("{{TOKEN}}", html.escape(cfg["token"]))
            .replace("{{UPSTREAM}}", html.escape(cfg["upstream"])))
    resp = web.Response(text=page, content_type="text/html")
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
        slots = int(cfg.get("upstream_concurrency") or 0)
        # 0 means "do not throttle"; a Semaphore(0) would deadlock.
        app[UPSTREAM_GATE] = (asyncio.Semaphore(slots) if slots > 0 else None)
        if cfg.get("enabled"):
            # Warm the catalog in the background: right after a restart
            # /health and the phone page then show real models instead of
            # "未探测 / 0 个" until someone happens to call.
            app[WARM] = asyncio.create_task(_warm_catalog(app))

    async def _on_cleanup(app: web.Application) -> None:
        task = app.get(WARM)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await app[SESSION].close()

    app.on_startup.append(_on_start)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_post("/v1/messages", _proxy)
    # Same upstream, OpenAI wire format: most phone LLM clients only speak it.
    app.router.add_post("/v1/chat/completions", _openai_chat)
    app.router.add_get("/v1/models", _proxy)
    # Any other /v1 path (an OpenAI-compatible client, a typo) must still get
    # an answer that says what this gateway speaks, not a bare 404 page.
    app.router.add_route("OPTIONS", "/v1/{tail:.*}", _preflight)
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
    try:
        # site.start() 必须在 try 里面：端口被占用时它会抛，而 aiohttp 的
        # ClientSession 是在 on_startup 里建的。原来 cleanup 在 try 之外，
        # bind 失败就没人关 session，退出时会打「Unclosed client session」。
        site = web.TCPSite(runner, "0.0.0.0", cfg["port"])
        await site.start()
        # The full token never goes to a log file; the GUI/CLI show it on demand.
        print(f"[models] gateway on http://0.0.0.0:{cfg['port']} -> "
              f"{cfg['upstream']}  token: {mask_token(cfg['token'])}", flush=True)
        if ready:
            ready(cfg)
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
            payload = json.loads(resp.read().decode())
            # 未授权的 /health 只回 {"ok": true}，没有 models/stats 等字段。
            # 不区分的话，上层会把它读成「网关活着但 0 个模型」，看着像故障。
            if token and "models" not in payload and "stats" not in payload:
                return {"listening": True, "unauthorized": True, **payload}
            return {"listening": True, **payload}
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


def _catalog_for(cfg: dict) -> dict:
    """Catalog for the CLI/GUI: cached copy, refreshed at most once per click."""
    return catalog_sync(cfg, timeout=2.0)


def _rows_from_catalog(catalog: dict) -> list[dict]:
    rows = []
    for model_id in visible_model_ids(catalog):
        rows.append({"id": model_id,
                     "short": _short_model(model_id),
                     "display": catalog.get("display", {}).get(model_id,
                                                               model_id)})
    return rows


def cached_models(store) -> list[dict]:
    """Catalog rows from the in-process cache only: no network on the GUI thread."""
    cfg = load_config(store)
    return _rows_from_catalog(_cached_catalog(cfg["upstream"].rstrip("/"))
                              or _empty_catalog())


def _rows_from_gateway(cfg: dict, timeout: float = 5.0) -> list[dict]:
    """Ask the *running* gateway for its model list.

    The gateway knows which routes just died; a freshly started CLI process does
    not, so filtering locally would show models that the gateway would refuse.
    """
    import urllib.request
    request = urllib.request.Request(
        f"http://127.0.0.1:{int(cfg['port'])}/v1/models",
        headers={"x-api-key": cfg["token"]})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return []
    rows = []
    for row in payload.get("data") or []:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        model_id = str(row["id"])
        rows.append({"id": model_id, "short": _short_model(model_id),
                     "display": str(row.get("display_name") or model_id)})
    return rows


def models_list(store) -> list[dict]:
    """Ids + display names for `flix models list` and the GUI copy button."""
    cfg = load_config(store)
    live = probe(cfg["port"], cfg["token"], timeout=2.0)
    if live.get("listening"):
        rows = _rows_from_gateway(cfg)
        if rows:
            return rows
    return _rows_from_catalog(_catalog_for(cfg))


def status_payload(store) -> dict:
    cfg = load_config(store)
    live = probe(cfg["port"], cfg["token"])
    catalog = _cached_catalog(cfg["upstream"].rstrip("/")) or {}
    listening = bool(live.get("listening"))
    # A fresh CLI process has an empty local cache; the running gateway's
    # /health knows how many models it last served, so prefer that.
    models = live.get("models") if listening else None
    if models is None:
        models = len(catalog.get("ids") or [])
    routes = live.get("routes") if listening else None
    prefixes = live.get("relay_prefixes") if listening else None
    return {"ok": True, "enabled": bool(cfg["enabled"]), "port": cfg["port"],
            "upstream": cfg["upstream"], "token": cfg["token"],
            "max_body_mb": cfg["max_body_mb"],
            "max_concurrent": cfg.get("max_concurrent", DEFAULT_MAX_CONCURRENT),
            # 上游闸门与排队时长：和 max_concurrent（本机总并发）是两个概念，
            # status 要把它们分开显示，否则「无限制」会让人以为没有上限。
            "upstream_concurrency": cfg.get("upstream_concurrency"),
            "queue_wait_s": cfg.get("queue_wait_s"),
            "unauthorized": bool(live.get("unauthorized")),
            "allow_remote_upstream": cfg["allow_remote_upstream"],
            "urls": lan_urls(cfg["port"]), "pair": pair_text(cfg),
            "urls_named": lan_urls_named(cfg["port"]),
            "models": models,
            "model_ids": (catalog.get("ids") or [])[:40],
            "routes": routes if routes is not None else route_health(),
            # The CLI's own process has no knowledge of what the running
            # gateway has probed; take it from /health when it is up.
            "relay_prefixes": (prefixes if prefixes is not None
                               else relay_prefix_health()),
            "upstream_timeout_s": cfg.get("upstream_timeout_s"),
            "listening": listening,
            "upstream_ok": live.get("upstream_ok"),
            "last_error": live.get("last_error", ""),
            "stats": live.get("stats", {}),
            "detail": live.get("detail", "")}
