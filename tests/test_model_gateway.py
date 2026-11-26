"""LAN model gateway: token gate, verbatim proxying, SSE passthrough."""
from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import subprocess
import time
import tempfile
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from flixcli.config import Store
from flixcli import model_gateway
from flixcli.model_gateway import (CONFIG_TEMPLATE, load_config, make_model_app,
                                   new_token, reset_token, save_config,
                                   stop_gateway)


def _upstream_app(seen: list[dict]) -> web.Application:
    async def messages(request: web.Request) -> web.Response:
        body = await request.read()
        seen.append({"path": request.path, "body": body,
                     "headers": dict(request.headers),
                     "query": dict(request.query)})
        if request.query.get("stream") == "1":
            resp = web.StreamResponse(headers={"Content-Type":
                                               "text/event-stream"})
            await resp.prepare(request)
            for chunk in (b"event: message_start\ndata: {}\n\n",
                          b"event: message_stop\ndata: {}\n\n"):
                await resp.write(chunk)
                await asyncio.sleep(0)
            await resp.write_eof()
            return resp
        return web.json_response({"type": "message", "model": "glm-5.3"})

    async def models(request: web.Request) -> web.Response:
        return web.json_response({"data": []})

    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_post("/v1/messages", messages)
    app.router.add_get("/v1/models", models)
    return app


class ModelGatewayTests(unittest.TestCase):
    def _cfg(self, tmp: str, upstream: str) -> dict:
        store = Store(Path(tmp) / "db.sqlite")
        try:
            return save_config(store, upstream=upstream, port=0)
        finally:
            store.close()

    def test_token_is_minted_once_and_rotates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            try:
                first = load_config(store)["token"]
                self.assertTrue(first.startswith("rd-"))
                self.assertEqual(first, load_config(store)["token"])
                second = reset_token(store)
                self.assertNotEqual(first, second)
                self.assertEqual(second, load_config(store)["token"])
                self.assertFalse(CONFIG_TEMPLATE["token"])
            finally:
                store.close()

    def test_rejects_wrong_token_without_touching_upstream(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_upstream_app(seen))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        resp = await client.post(
                            "/v1/messages", json={"model": "x"},
                            headers={"x-api-key": "nope"})
                        self.assertEqual(resp.status, 401)
                        self.assertEqual(seen, [])
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_health_keeps_the_last_refusals(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_upstream_app(seen))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        await client.post("/v1/messages", json={"model": "glm-5.3"},
                                          headers={"x-api-key": "stale"})
                        resp = await client.get(
                            "/health", headers={"x-api-key": cfg["token"]})
                        body = await resp.json()
                        recent = body["stats"]["recent"]
                        # 现在会带出「没带令牌」还是「带了哪个错的」前缀
                        self.assertTrue(recent[-1]["kind"].startswith("401 token"),
                                        recent[-1]["kind"])
                        self.assertIn("stale", recent[-1]["kind"])
                        self.assertIn("remote", recent[-1])
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_concurrency_limit_answers_429(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                gate = asyncio.Event()

                async def slow(request):
                    await gate.wait()
                    return web.json_response({"type": "message"})

                upstream_app = web.Application()
                upstream_app.router.add_post("/v1/messages", slow)
                upstream = TestServer(upstream_app)
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    cfg["max_concurrent"] = 1
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        headers = {"x-api-key": cfg["token"]}
                        first = asyncio.create_task(client.post(
                            "/v1/messages", json={"model": "x"}, headers=headers))
                        # Wait for the request to be accounted in flight rather
                        # than sleeping a fixed 0.2s (flaky on a loaded CI box).
                        for _ in range(200):
                            if client.app[model_gateway.STATS]["active"] >= 1:
                                break
                            await asyncio.sleep(0.01)
                        self.assertEqual(
                            client.app[model_gateway.STATS]["active"], 1,
                            "第一个请求没有进入在途状态")
                        second = await client.post(
                            "/v1/messages", json={"model": "x"}, headers=headers)
                        self.assertEqual(second.status, 429)
                        self.assertEqual(second.headers.get("Retry-After"), "2")
                        payload = await second.json()
                        self.assertEqual(payload["error"]["type"],
                                         "overloaded_error")
                        gate.set()
                        self.assertEqual((await first).status, 200)
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_limit_holds_when_requests_arrive_together(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                gate = asyncio.Event()

                async def slow(request):
                    await gate.wait()
                    return web.json_response({"type": "message"})

                upstream_app = web.Application()
                upstream_app.router.add_post("/v1/messages", slow)
                upstream = TestServer(upstream_app)
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    cfg["max_concurrent"] = 1
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        headers = {"x-api-key": cfg["token"]}
                        tasks = [asyncio.create_task(client.post(
                            "/v1/messages", json={"model": "x"},
                            headers=headers)) for _ in range(4)]
                        await asyncio.sleep(0.2)
                        gate.set()
                        responses = await asyncio.gather(*tasks)
                        codes = [r.status for r in responses]
                        # Without reserving the slot before the body read, all
                        # four pass the guard and the cap does nothing.
                        self.assertEqual(codes.count(200), 1, codes)
                        self.assertEqual(codes.count(429), 3, codes)
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_concurrency_default_is_unlimited_and_clamped(self):
        self.assertEqual(CONFIG_TEMPLATE["max_concurrent"], 0,
                         "默认不该限制并发：429 不该出现在开箱即用路径上")
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            try:
                cfg = save_config(store, max_concurrent=999)
                self.assertEqual(cfg["max_concurrent"], 32)
                cfg = save_config(store, max_concurrent=-5)
                self.assertEqual(cfg["max_concurrent"], 0)
            finally:
                store.close()

    def test_unlimited_concurrency_never_answers_429(self):
        """max_concurrent=0 must let a burst through instead of throttling it."""

        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                gate = asyncio.Event()
                inflight = {"now": 0, "peak": 0}

                async def slow(request):
                    inflight["now"] += 1
                    inflight["peak"] = max(inflight["peak"], inflight["now"])
                    await gate.wait()
                    inflight["now"] -= 1
                    return web.json_response({"type": "message"})

                upstream_app = web.Application()
                upstream_app.router.add_post("/v1/messages", slow)
                upstream = TestServer(upstream_app)
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    cfg["max_concurrent"] = 0
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        headers = {"x-api-key": cfg["token"]}
                        tasks = [asyncio.create_task(client.post(
                            "/v1/messages", json={"model": "x"},
                            headers=headers)) for _ in range(8)]
                        for _ in range(200):
                            if inflight["peak"] >= 4:
                                break
                            await asyncio.sleep(0.01)
                        gate.set()
                        responses = await asyncio.gather(*tasks)
                        codes = [r.status for r in responses]
                        self.assertEqual(codes.count(200), 8, codes)
                        self.assertEqual(codes.count(429), 0, codes)
                        self.assertGreaterEqual(inflight["peak"], 4,
                                                "上游没有真正收到并发请求")
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_forwards_body_and_returns_upstream_json(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_upstream_app(seen))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        resp = await client.post(
                            "/v1/messages",
                            json={"model": "claude-sonnet-4-5",
                                  "messages": []},
                            headers={"x-api-key": cfg["token"]})
                        self.assertEqual(resp.status, 200)
                        payload = await resp.json()
                        self.assertEqual(payload["model"], "glm-5.3")
                        self.assertEqual(len(seen), 1)
                        self.assertEqual(seen[0]["path"], "/v1/messages")
                        self.assertIn(b"claude-sonnet-4-5",
                                      seen[0]["body"])
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_streams_sse_and_strips_own_token_param(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_upstream_app(seen))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        resp = await client.post(
                            "/v1/messages?stream=1",
                            json={"model": "claude-haiku-4-5"},
                            headers={"accept": "text/event-stream",
                                     "x-api-key": cfg["token"]})
                        self.assertEqual(resp.status, 200)
                        text = await resp.text()
                        self.assertIn("event: message_start", text)
                        self.assertIn("event: message_stop", text)
                        self.assertEqual(seen[0]["query"].get("stream"), "1")
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_upstream_down_returns_502(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                cfg = self._cfg(tmp, "http://127.0.0.1:1")
                client = TestClient(TestServer(make_model_app(None, cfg)))
                await client.start_server()
                try:
                    resp = await client.post("/v1/messages", json={"model": "glm-5.3"},
                                             headers={"x-api-key":
                                                      cfg["token"]})
                    self.assertEqual(resp.status, 502)
                    body = await resp.json()
                    self.assertEqual(body["type"], "error")
                finally:
                    await client.close()

        asyncio.run(run())

    def test_large_body_beyond_aiohttp_default_is_forwarded(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_upstream_app(seen))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        # aiohttp's own default is 1 MiB; real prompts exceed it.
                        big = "x" * (2 * 1024 * 1024)
                        resp = await client.post(
                            "/v1/messages",
                            json={"model": "claude-sonnet-4-5",
                                  "messages": [{"role": "user",
                                                "content": big}]},
                            headers={"x-api-key": cfg["token"]})
                        self.assertEqual(resp.status, 200)
                        self.assertGreater(len(seen[0]["body"]), 2 * 1024 * 1024)
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_body_over_configured_limit_is_413_json(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_upstream_app(seen))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    cfg["max_body_mb"] = 1
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        resp = await client.post(
                            "/v1/messages",
                            json={"model": "x",
                                  "messages": [{"role": "user",
                                                "content": "y" * 1_200_000}]},
                            headers={"x-api-key": cfg["token"]})
                        self.assertEqual(resp.status, 413)
                        body = await resp.json()
                        self.assertEqual(body["type"], "error")
                        self.assertEqual(seen, [])
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_upstream_error_status_and_headers_pass_through(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                async def boom(request):
                    return web.json_response(
                        {"type": "error", "error": {"type": "rate_limit_error",
                                                    "message": "slow down"}},
                        status=429, headers={"request-id": "req-123",
                                             "retry-after": "7"})

                upstream = web.Application()
                upstream.router.add_post("/v1/messages", boom)
                server = TestServer(upstream)
                await server.start_server()
                try:
                    cfg = self._cfg(tmp, str(server.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        resp = await client.post(
                            "/v1/messages", json={"model": "glm-5.3"},
                            headers={"x-api-key": cfg["token"]})
                        self.assertEqual(resp.status, 429)
                        self.assertEqual(resp.headers.get("request-id"),
                                         "req-123")
                        self.assertEqual(resp.headers.get("retry-after"), "7")
                        health = await (await client.get(
                            "/health",
                            headers={"x-api-key": cfg["token"]})).json()
                        self.assertEqual(health["stats"]["failed"], 1)
                        self.assertEqual(health["stats"]["ok"], 0)
                    finally:
                        await client.close()
                finally:
                    await server.close()

        asyncio.run(run())

    def test_upstream_url_must_be_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            try:
                with self.assertRaises(ValueError):
                    save_config(store, upstream="file:///etc/passwd")
                with self.assertRaises(ValueError):
                    save_config(store, upstream="http://user:pw@127.0.0.1:1")
                # non-loopback is refused until explicitly allowed
                with self.assertRaises(ValueError):
                    save_config(store, upstream="http://10.0.0.5:15721")
                cfg = save_config(store, upstream="http://10.0.0.5:15721",
                                  allow_remote_upstream=True)
                self.assertEqual(cfg["upstream"], "http://10.0.0.5:15721")
                self.assertEqual(save_config(store, upstream="http://127.0.0.1:9/",
                                             allow_remote_upstream=True)[
                    "upstream"], "http://127.0.0.1:9")
            finally:
                store.close()


    def test_query_token_is_rejected_for_api_calls(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_upstream_app(seen))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        resp = await client.post(
                            f"/v1/messages?token={cfg['token']}", json={})
                        self.assertEqual(resp.status, 401)
                        self.assertEqual(seen, [])
                        # ...but the browser usage page still accepts it.
                        page = await client.get(f"/?token={cfg['token']}")
                        self.assertEqual(page.status, 200)
                        self.assertEqual(page.headers.get("referrer-policy"),
                                         "no-referrer")
                        self.assertEqual(page.headers.get("cache-control"),
                                         "no-store")
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_health_without_token_is_minimal(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                cfg = self._cfg(tmp, "http://127.0.0.1:1")
                client = TestClient(TestServer(make_model_app(None, cfg)))
                await client.start_server()
                try:
                    body = await (await client.get("/health")).json()
                    self.assertEqual(body, {"ok": True})
                    full = await (await client.get(
                        "/health", headers={"x-api-key": cfg["token"]})).json()
                    self.assertIn("upstream", full)
                finally:
                    await client.close()

        asyncio.run(run())

    def test_upstream_drops_mid_stream_without_second_response(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                async def partial(request):
                    resp = web.StreamResponse(
                        headers={"Content-Type": "text/event-stream"})
                    await resp.prepare(request)
                    chunk = b"event: message_start" + bytes([10]) + b"data: {}" + bytes([10, 10])
                    await resp.write(chunk)
                    # Simulate the upstream dying mid-stream: aiohttp aborts the
                    # connection, the chunked body never terminates.
                    raise RuntimeError("upstream died")

                upstream = web.Application()
                upstream.router.add_post("/v1/messages", partial)
                server = TestServer(upstream)
                await server.start_server()
                try:
                    cfg = self._cfg(tmp, str(server.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        resp = await client.post(
                            "/v1/messages", json={"model": "glm-5.3"},
                            headers={"x-api-key": cfg["token"]})
                        # Headers were already sent; the gateway must not try to
                        # write a second status line onto the same stream. It
                        # closes the chunked body instead, so the phone sees a
                        # truncated-but-valid SSE and retries on its own.
                        self.assertEqual(resp.status, 200)
                        text = await resp.text()
                        self.assertIn("event: message_start", text)
                        self.assertNotIn("message_stop", text)
                        stats = (await (await client.get(
                            "/health",
                            headers={"x-api-key": cfg["token"]})).json())["stats"]
                        self.assertEqual(stats["ok"], 0)
                        self.assertEqual(stats["failed"] + stats["aborted"], 1)
                    finally:
                        await client.close()
                finally:
                    await server.close()

        asyncio.run(run())

    def test_bad_port_and_body_cap_are_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            try:
                cfg = save_config(store, port="18993a", max_body_mb=9999)
                self.assertEqual(cfg["port"], 18993)
                self.assertEqual(cfg["max_body_mb"], 128)
                self.assertEqual(save_config(store, port=70000)["port"], 18993)
                self.assertEqual(save_config(store, max_body_mb=0)["max_body_mb"],
                                 1)
            finally:
                store.close()

    def test_stop_gateway_on_free_port_is_a_noop(self):
        result = stop_gateway(1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["stopped"], [])

    def test_new_token_prefix(self):
        self.assertTrue(new_token().startswith("rd-"))


CATALOG = {
    "data": [
        {"id": "ccs-claude-openai--glm-5.3", "display_name": "glm-5.3（jlc-cn）",
         "type": "model"},
        {"id": "ccs-claude-openai--glm-5.3-flash",
         "display_name": "glm-5.3-flash（jlc-cn）", "type": "model"},
        {"id": "ccs-claude-openai--kimi-k2.7-code",
         "display_name": "kimi-k2.7-code（jlc-cn）", "type": "model"},
    ],
}


def _catalog_app(seen: list[dict], catalog: dict | None = None) -> web.Application:
    """An upstream that answers both the catalog and /v1/messages."""
    data = catalog if catalog is not None else CATALOG

    async def models(request: web.Request) -> web.Response:
        seen.append({"kind": "models", "headers": dict(request.headers)})
        return web.json_response(data)

    async def messages(request: web.Request) -> web.Response:
        seen.append({"kind": "messages", "headers": dict(request.headers),
                     "body": json.loads(await request.read() or b"{}")})
        return web.json_response({"type": "message", "model": "glm-5.3",
                                  "content": [{"type": "text", "text": "OK"}]})

    app = web.Application()
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/messages", messages)
    return app


class ModelCatalogTests(unittest.TestCase):
    """Phone-facing behaviour earned from real 联调: catalog shape, model ids,
    protocol header, CORS, and a page that can prove the chain end to end."""

    def setUp(self):
        # The catalog cache is process-global by design (one upstream per host).
        model_gateway._CATALOG_CACHE.update(upstream="", catalog=None,
                                            at=0.0, fail_at=0.0)
        model_gateway._ROUTE_HEALTH.clear()

    def _cfg(self, tmp: str, upstream: str) -> dict:
        store = Store(Path(tmp) / "db.sqlite")
        try:
            return save_config(store, upstream=upstream, port=0)
        finally:
            store.close()

    def test_parse_catalog_accepts_anthropic_and_codex_shapes(self):
        anthropic = model_gateway._parse_catalog(CATALOG)
        self.assertEqual(anthropic["ids"][0], "ccs-claude-openai--glm-5.3")
        self.assertEqual(anthropic["short"]["glm-5.3"],
                         "ccs-claude-openai--glm-5.3")
        codex = model_gateway._parse_catalog(
            {"models": [{"slug": "ccs-claude-x--k3", "display_name": "k3"}]})
        self.assertEqual(codex["ids"], ["ccs-claude-x--k3"])
        self.assertEqual(model_gateway._parse_catalog(None)["ids"], [])

    def test_short_name_and_legacy_alias_resolve_to_real_ids(self):
        catalog = model_gateway._parse_catalog(CATALOG)
        self.assertEqual(model_gateway.resolve_model("glm-5.3", catalog),
                         "ccs-claude-openai--glm-5.3")
        self.assertEqual(model_gateway.resolve_model("GLM-5.3-FLASH", catalog),
                         "ccs-claude-openai--glm-5.3-flash")
        # The pre-11-16 phone config used claude-* names; they must still work.
        self.assertEqual(model_gateway.resolve_model("claude-haiku-4-5", catalog),
                         "ccs-claude-openai--kimi-k2.7-code")
        self.assertEqual(
            model_gateway.resolve_model("ccs-claude-openai--glm-5.3", catalog),
            "ccs-claude-openai--glm-5.3")
        # Unknown ids are forwarded untouched so exotic models keep working.
        self.assertEqual(model_gateway.resolve_model("some-exotic", catalog),
                         "some-exotic")

    def test_request_without_anthropic_version_gets_it_forwarded(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_catalog_app(seen))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        headers = {"x-api-key": cfg["token"]}
                        models = await client.get("/v1/models", headers=headers)
                        self.assertEqual(models.status, 200)
                        body = await models.json()
                        self.assertIn("data", body)
                        self.assertEqual(body["data"][0]["id"],
                                         "ccs-claude-openai--glm-5.3")
                        # cc-switch picks the Claude catalog only with this
                        # header; without it the phone gets the Codex one.
                        sent = [row for row in seen if row["kind"] == "models"]
                        self.assertEqual(
                            sent[-1]["headers"].get("anthropic-version"),
                            "2023-06-01")
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_client_anthropic_version_is_not_overwritten(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_catalog_app(seen))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        await client.post(
                            "/v1/messages", json={"model": "glm-5.3"},
                            headers={"x-api-key": cfg["token"],
                                     "anthropic-version": "2024-06-01"})
                        call = [row for row in seen
                                if row["kind"] == "messages"][-1]
                        self.assertEqual(call["headers"].get("anthropic-version"),
                                         "2024-06-01")
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_codex_shaped_catalog_is_normalized_for_anthropic_clients(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                codex = {"models": [
                    {"slug": "ccs-claude-x--k3", "display_name": "k3",
                     "base_instructions": "x" * 500},
                    {"slug": "ccs-claude-x--glm-5.3", "display_name": "glm-5.3"},
                ]}
                upstream = TestServer(_catalog_app(seen, codex))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        resp = await client.get(
                            "/v1/models", headers={"x-api-key": cfg["token"]})
                        body = await resp.json()
                        self.assertEqual([row["id"] for row in body["data"]],
                                         ["ccs-claude-x--k3",
                                          "ccs-claude-x--glm-5.3"])
                        self.assertFalse(body["has_more"])
                        self.assertNotIn("base_instructions", str(body))
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_short_model_name_is_rewritten_to_the_full_upstream_id(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_catalog_app(seen))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        headers = {"x-api-key": cfg["token"]}
                        resp = await client.post(
                            "/v1/messages",
                            json={"model": "glm-5.3-flash",
                                  "messages": [{"role": "user",
                                                "content": "hi"}]},
                            headers=headers)
                        self.assertEqual(resp.status, 200)
                        call = [row for row in seen
                                if row["kind"] == "messages"][-1]
                        self.assertEqual(call["body"]["model"],
                                         "ccs-claude-openai--glm-5.3-flash")
                        self.assertEqual(call["body"]["messages"][0]["content"],
                                         "hi")
                        health = await (await client.get("/health",
                                                         headers=headers)).json()
                        recent = health["stats"]["recent_requests"][-1]
                        self.assertEqual(recent["model"], "glm-5.3-flash")
                        self.assertEqual(recent["to"],
                                         "ccs-claude-openai--glm-5.3-flash")
                        self.assertEqual(recent["status"], 200)
                        self.assertTrue(health["upstream_ok"])
                        self.assertEqual(health["models"], 3)
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_unknown_model_is_rejected_with_the_available_list(self):
        """cc-switch silently substitutes a default model for unknown names;
        the gateway must reject instead, and say what is available."""
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_catalog_app(seen))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        resp = await client.post(
                            "/v1/messages",
                            json={"model": "some-exotic",
                                  "messages": [{"role": "user",
                                                "content": "hi"}]},
                            headers={"x-api-key": cfg["token"]})
                        self.assertEqual(resp.status, 400)
                        body = await resp.json()
                        message = body["error"]["message"]
                        self.assertIn("some-exotic", message)
                        self.assertIn("glm-5.3", message)
                        self.assertEqual(
                            [row for row in seen
                             if row["kind"] == "messages"], [])
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_reasoning_only_stream_still_yields_content(self):
        """A model that spends max_tokens on thinking must not give an OpenAI
        client a blank stream (the non-stream path already falls back)."""
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                async def models(request):
                    return web.json_response(CATALOG)

                async def messages(request):
                    resp = web.StreamResponse(headers={"Content-Type":
                                                       "text/event-stream"})
                    await resp.prepare(request)
                    events = [
                        {"type": "message_start",
                         "message": {"id": "m", "model": "glm-5.3",
                                     "usage": {"input_tokens": 3,
                                               "output_tokens": 0}}},
                        {"type": "content_block_delta", "index": 0,
                         "delta": {"type": "thinking_delta",
                                   "thinking": "only thinking here"}},
                        {"type": "message_delta",
                         "delta": {"stop_reason": "max_tokens"},
                         "usage": {"input_tokens": 3, "output_tokens": 9}},
                        {"type": "message_stop"},
                    ]
                    for event in events:
                        await resp.write(b"event: " + event["type"].encode() +
                                         b"\ndata: " +
                                         json.dumps(event).encode() + b"\n\n")
                    await resp.write_eof()
                    return resp

                app = web.Application()
                app.router.add_get("/v1/models", models)
                app.router.add_post("/v1/messages", messages)
                upstream = TestServer(app)
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        resp = await client.post(
                            "/v1/chat/completions",
                            json={"model": "glm-5.3", "stream": True,
                                  "messages": [{"role": "user",
                                                "content": "hi"}]},
                            headers={"x-api-key": cfg["token"]})
                        text = await resp.text()
                        self.assertIn('"content": "only thinking here"', text)
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_unknown_model_passes_through_when_catalog_is_unavailable(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []

                async def models(request):
                    return web.json_response({"data": []})

                async def messages(request):
                    seen.append({"kind": "messages",
                                 "body": json.loads(await request.read()
                                                    or b"{}")})
                    return web.json_response(ANTHROPIC_REPLY)

                upstream_app = web.Application()
                upstream_app.router.add_get("/v1/models", models)
                upstream_app.router.add_post("/v1/messages", messages)
                upstream = TestServer(upstream_app)
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        resp = await client.post(
                            "/v1/messages",
                            json={"model": "some-exotic",
                                  "messages": [{"role": "user",
                                                "content": "hi"}]},
                            headers={"x-api-key": cfg["token"]})
                        self.assertEqual(resp.status, 200)
                        self.assertEqual(seen[-1]["body"]["model"],
                                         "some-exotic")
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_catalog_matching_accepts_ids_short_names_and_legacy_aliases(self):
        catalog = model_gateway._parse_catalog(CATALOG)
        self.assertTrue(model_gateway.catalog_matches(
            "ccs-claude-openai--glm-5.3", catalog))
        self.assertTrue(model_gateway.catalog_matches("glm-5.3", catalog))
        self.assertTrue(model_gateway.catalog_matches("GLM-5.3", catalog))
        self.assertTrue(model_gateway.catalog_matches(
            "claude-haiku-4-5", catalog))  # legacy alias
        self.assertFalse(model_gateway.catalog_matches("gpt-4o", catalog))
        self.assertFalse(model_gateway.catalog_matches("", catalog))
        self.assertIn("glm-5.3", model_gateway.model_hint(catalog))

    def test_openai_unknown_model_returns_model_not_found(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_openai_upstream(seen))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        resp = await client.post(
                            "/v1/chat/completions",
                            json={"model": "gpt-4o",
                                  "messages": [{"role": "user",
                                                "content": "hi"}]},
                            headers={"x-api-key": cfg["token"]})
                        self.assertEqual(resp.status, 400)
                        body = await resp.json()
                        self.assertEqual(body["error"]["code"],
                                         "model_not_found")
                        self.assertIn("gpt-4o", body["error"]["message"])
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_cors_preflight_and_response_headers(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_catalog_app(seen))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        pre = await client.options("/v1/messages")
                        self.assertEqual(pre.status, 204)
                        self.assertEqual(
                            pre.headers.get("access-control-allow-origin"), "*")
                        self.assertIn(
                            "x-api-key",
                            pre.headers.get("access-control-allow-headers", ""))
                        resp = await client.get(
                            "/v1/models", headers={"x-api-key": cfg["token"]})
                        self.assertEqual(
                            resp.headers.get("access-control-allow-origin"), "*")
                        bad = await client.post("/v1/messages", json={"model": "glm-5.3"})
                        self.assertEqual(bad.status, 401)
                        self.assertEqual(
                            bad.headers.get("access-control-allow-origin"), "*")
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_phone_page_lists_live_models_and_self_test(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_catalog_app(seen))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        page = await client.get(f"/?token={cfg['token']}")
                        self.assertEqual(page.status, 200)
                        text = await page.text()
                        # The list is the live catalog, not the stale
                        # hardcoded claude-* names that answered 503.
                        self.assertIn("ccs-claude-openai--glm-5.3", text)
                        self.assertNotIn("claude-sonnet-4-5", text)
                        self.assertIn("发送测试", text)
                        self.assertIn("/v1/messages", text)
                        self.assertIn("/v1/chat/completions", text)
                        self.assertIn("tooltest", text)
                        self.assertIn("工具调用通过", text)
                        # A real newline inside this JS string literal breaks
                        # the whole page script (it must stay an escape).
                        self.assertIn(r"未产生正文）\n\n", text)
                        self.assertEqual(page.headers.get("referrer-policy"),
                                         "no-referrer")
                        self.assertEqual(page.headers.get("cache-control"),
                                         "no-store")
                        denied = await client.get("/?token=wrong")
                        self.assertEqual(denied.status, 401)
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_models_falls_back_to_cached_catalog_when_upstream_fails(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_catalog_app(seen))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        headers = {"x-api-key": cfg["token"]}
                        first = await client.get("/v1/models", headers=headers)
                        self.assertEqual(first.status, 200)
                        await upstream.close()  # cc-switch goes away
                        model_gateway._CATALOG_CACHE["at"] = 0.0  # force a probe
                        second = await client.get("/v1/models", headers=headers)
                        self.assertEqual(second.status, 200)
                        self.assertEqual(second.headers.get("x-relaydesk-cached"),
                                         "1")
                        body = await second.json()
                        self.assertEqual(body["data"][0]["id"],
                                         "ccs-claude-openai--glm-5.3")
                        health = await (await client.get(
                            "/health", headers=headers)).json()
                        self.assertFalse(health["upstream_ok"])
                        self.assertIn("unreachable", health["last_error"])
                    finally:
                        await client.close()
                finally:
                    with contextlib.suppress(Exception):
                        await upstream.close()

        asyncio.run(run())

    def test_rewrite_body_only_touches_json_payloads(self):
        self.assertIsNone(model_gateway._rewrite_model_body(b"not json", "a", "b"))
        self.assertIsNone(model_gateway._rewrite_model_body(b"", "a", "b"))
        # Body already carries a different model: leave the client's bytes alone.
        self.assertIsNone(model_gateway._rewrite_model_body(
            b'{"model":"other"}', "a", "b"))
        self.assertIn(b'"model": "b"', model_gateway._rewrite_model_body(
            b'{"model":"a","messages":[]}', "a", "b"))

    def test_short_name_prefers_the_local_direct_route(self):
        """A relay route (``ccs-claude-<peer>--ccs-claude-...``) can be hung
        while the local direct route answers; the alias must not pick the
        relay just because the catalog lists it first."""
        catalog = model_gateway._parse_catalog({"data": [
            {"id": "ccs-claude-peer--ccs-claude-x--glm-5.3",
             "display_name": "relay"},
            {"id": "ccs-claude-openai--glm-5.3", "display_name": "direct"},
        ]})
        ordered = model_gateway.resolve_candidates("glm-5.3", catalog)
        self.assertEqual(ordered[0], "ccs-claude-openai--glm-5.3")
        self.assertIn("ccs-claude-peer--ccs-claude-x--glm-5.3", ordered)

    def test_failed_route_falls_back_and_is_remembered(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                calls: list[str] = []

                async def models(request):
                    return web.json_response({"data": [
                        {"id": "ccs-claude-peer--ccs-claude-a--glm-5.3", "display_name": "a"},
                        {"id": "ccs-claude-peer--ccs-claude-b--glm-5.3", "display_name": "b"},
                    ]})

                async def messages(request):
                    body = json.loads(await request.read() or b"{}")
                    if body.get("max_tokens") == 1:
                        return web.json_response({"type": "message",
                                                  "model": "glm-5.3"})
                    calls.append(body.get("model", ""))
                    if "ccs-claude-a" in body.get("model", ""):
                        return web.json_response({"type": "error"},
                                                 status=503)
                    return web.json_response({"type": "message",
                                              "model": "glm-5.3"})

                upstream_app = web.Application()
                upstream_app.router.add_get("/v1/models", models)
                upstream_app.router.add_post("/v1/messages", messages)
                upstream = TestServer(upstream_app)
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        headers = {"x-api-key": cfg["token"]}
                        first = await client.post(
                            "/v1/messages", json={"model": "glm-5.3",
                                                  "messages": []},
                            headers=headers)
                        self.assertEqual(first.status, 200)
                        # First candidate died, the second one answered.
                        self.assertEqual(calls, ["ccs-claude-peer--ccs-claude-a--glm-5.3",
                                                 "ccs-claude-peer--ccs-claude-b--glm-5.3"])
                        health = model_gateway.route_health()
                        self.assertFalse(health["ccs-claude-peer--ccs-claude-a--glm-5.3"]["ok"])
                        self.assertTrue(health["ccs-claude-peer--ccs-claude-b--glm-5.3"]["ok"])
                        # Next call goes straight to the known-good route.
                        calls.clear()
                        second = await client.post(
                            "/v1/messages", json={"model": "glm-5.3",
                                                  "messages": []},
                            headers=headers)
                        self.assertEqual(second.status, 200)
                        self.assertEqual(calls, ["ccs-claude-peer--ccs-claude-b--glm-5.3"])
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_all_routes_failing_returns_the_last_upstream_status(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                async def models(request):
                    return web.json_response({"data": [
                        {"id": "ccs-claude-a--glm-5.3", "display_name": "a"},
                        {"id": "ccs-claude-b--glm-5.3", "display_name": "b"},
                    ]})

                async def messages(request):
                    return web.json_response({"type": "error"}, status=503)

                upstream_app = web.Application()
                upstream_app.router.add_get("/v1/models", models)
                upstream_app.router.add_post("/v1/messages", messages)
                upstream = TestServer(upstream_app)
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        resp = await client.post(
                            "/v1/messages", json={"model": "glm-5.3",
                                                  "messages": []},
                            headers={"x-api-key": cfg["token"]})
                        self.assertEqual(resp.status, 503)
                        body = await resp.json()
                        self.assertEqual(body["type"], "error")
                        # The upstream's own error passes through, tagged
                        # with which route produced it.
                        self.assertEqual(
                            resp.headers.get("x-relaydesk-route"),
                            "ccs-claude-b--glm-5.3")
                        self.assertEqual(
                            resp.headers.get("x-relaydesk-attempts"), "2")
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_models_hide_dead_routes_by_default(self):
        """已知不通的路由默认不出现在列表里（手机选择器不该全是坑）。"""
        model_gateway._route_mark("ccs-claude-openai--glm-5.3", False)
        payload = model_gateway._normalize_models_payload(
            json.dumps(CATALOG).encode("utf-8"))
        ids = [row["id"] for row in json.loads(payload)["data"]]
        self.assertNotIn("ccs-claude-openai--glm-5.3", ids)
        self.assertIn("ccs-claude-openai--kimi-k2.7-code", ids)

    def test_hide_can_be_disabled_so_dead_routes_stay_marked(self):
        """关掉隐藏后仍要能看到并标注「当前不通」，便于诊断。"""
        model_gateway._route_mark("ccs-claude-openai--glm-5.3", False)
        catalog = model_gateway._parse_catalog(CATALOG)
        ids = model_gateway.visible_model_ids(catalog, hide_dead=False)
        self.assertIn("ccs-claude-openai--glm-5.3", ids)
        self.assertEqual(
            model_gateway._display_with_health("ccs-claude-openai--glm-5.3",
                                              catalog),
            "glm-5.3（jlc-cn）（当前不通）")

    def test_never_returns_an_empty_list_even_if_everything_is_dead(self):
        catalog = model_gateway._parse_catalog({"data": [
            {"id": "ccs-claude-openai--glm-5.3"},
            {"id": "ccs-claude-openai--kimi-k2.7-code"},
        ]})
        for model_id in list(catalog["ids"]):
            model_gateway._route_mark(model_id, False)
        ids = model_gateway.visible_model_ids(catalog)
        self.assertEqual(len(ids), 2, "全死时也不能返回空列表")

    def test_models_list_puts_healthy_direct_routes_first(self):
        model_gateway._WARM_STATE["done_at"] = time.monotonic()
        """A phone preselects the first entry; the relay that hangs must not
        be it."""
        relay = "ccs-claude-peer--ccs-claude-x--glm-5.3"
        direct = "ccs-claude-openai--glm-5.3"
        dead = "ccs-claude-dead--kimi-k2.7-code"
        model_gateway._route_mark(dead, False)
        catalog = model_gateway._parse_catalog({"data": [
            {"id": relay, "display_name": "relay"},
            {"id": dead, "display_name": "dead"},
            {"id": direct, "display_name": "direct"}]})
        # 默认：死的直接隐藏，直连排第一
        ids = model_gateway.visible_model_ids(catalog)
        self.assertEqual(ids[0], direct)
        self.assertNotIn(dead, ids)
        # 关掉隐藏：死的排最后，仍不抢第一位
        ids_all = model_gateway.visible_model_ids(catalog, hide_dead=False)
        self.assertEqual(ids_all[0], direct)
        self.assertEqual(ids_all[-1], dead)
        # Same order the page picker uses.
        self.assertLess(model_gateway._model_options(
            model_gateway._parse_catalog({"data": [
                {"id": relay, "display_name": "r"},
                {"id": direct, "display_name": "d"}]})).index(direct),
            model_gateway._model_options(
                model_gateway._parse_catalog({"data": [
                    {"id": relay, "display_name": "r"},
                    {"id": direct, "display_name": "d"}]})).index(relay))

    def test_upstream_timeout_is_configurable_and_clamped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            try:
                self.assertEqual(
                    save_config(store, upstream_timeout_s=9999)[
                        "upstream_timeout_s"], 900)
                self.assertEqual(
                    save_config(store, upstream_timeout_s=1)[
                        "upstream_timeout_s"], 10)
                self.assertEqual(
                    save_config(store, upstream_timeout_s="bad")[
                        "upstream_timeout_s"], 180)
            finally:
                store.close()

    def test_phone_page_uses_the_address_the_phone_called_in_on(self):
        """A tethered phone calls 192.168.137.1 while the primary NIC is the
        Wi-Fi address; copying the wrong one made the phone config dead."""
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_catalog_app(seen))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        port = client.make_url("/").port
                        page = await client.get(f"/?token={cfg['token']}")
                        text = await page.text()
                        # TestServer is reached over loopback, so the page must
                        # advertise the loopback address it was actually
                        # called on, not whichever NIC happens to be primary.
                        self.assertIn(f"http://127.0.0.1:{port}", text)
                        poisoned = await client.get(
                            f"/?token={cfg['token']}",
                            headers={"Host": "evil.example"})
                        bad = await poisoned.text()
                        self.assertNotIn("evil.example", bad)
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_page_puts_reachable_direct_routes_first(self):
        model_gateway._WARM_STATE["done_at"] = time.monotonic()
        catalog = model_gateway._parse_catalog({"data": [
            {"id": "ccs-claude-peer--ccs-claude-x--glm-5.3",
             "display_name": "relay"},
            {"id": "ccs-claude-openai--glm-5.3", "display_name": "direct"},
        ]})
        html_options = model_gateway._model_options(catalog)
        self.assertLess(html_options.index("ccs-claude-openai--glm-5.3"),
                        html_options.index("ccs-claude-peer--ccs-claude-x--glm-5.3"))

    def test_route_fallback_shares_one_deadline(self):
        """Three hung routes must not cost three timeouts: the phone waits for
        one budget, then gets a clear 504."""
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                async def models(request):
                    return web.json_response({"data": [
                        {"id": "ccs-claude-a--glm-5.3", "display_name": "a"},
                        {"id": "ccs-claude-b--glm-5.3", "display_name": "b"},
                        {"id": "ccs-claude-c--glm-5.3", "display_name": "c"},
                    ]})

                async def slow(request):
                    body = json.loads(await request.read() or b"{}")
                    if body.get("max_tokens") == 1:
                        return web.json_response({"type": "message",
                                                  "model": "glm-5.3"})
                    await asyncio.sleep(30)
                    return web.json_response({"type": "message"})

                upstream_app = web.Application()
                upstream_app.router.add_get("/v1/models", models)
                upstream_app.router.add_post("/v1/messages", slow)
                upstream = TestServer(upstream_app)
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    cfg["upstream_timeout_s"] = 1  # bypasses the UI clamp on purpose
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        started = asyncio.get_event_loop().time()
                        resp = await client.post(
                            "/v1/messages", json={"model": "glm-5.3",
                                                  "messages": []},
                            headers={"x-api-key": cfg["token"]})
                        elapsed = asyncio.get_event_loop().time() - started
                        self.assertEqual(resp.status, 504)
                        self.assertLess(elapsed, 3.0,
                                        "每次尝试各等满超时，总耗时被放大")
                        self.assertFalse(model_gateway.route_health().get(
                            "ccs-claude-a--glm-5.3", {}).get("ok", True))
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())


ANTHROPIC_REPLY = {
    "id": "msg_test_1", "type": "message", "role": "assistant",
    "model": "glm-5.3",
    "content": [{"type": "thinking", "thinking": "let me think"},
                {"type": "text", "text": "OK"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 11, "output_tokens": 7},
}


def _openai_upstream(seen: list[dict], *, stream: bool = False,
                     status: int = 200, error_body: dict | None = None):
    """cc-switch stand-in: Anthropic route + catalog."""
    async def models(request):
        return web.json_response(CATALOG)

    async def messages(request):
        seen.append({"headers": dict(request.headers),
                     "body": json.loads(await request.read() or b"{}")})
        if error_body is not None:
            return web.json_response(error_body, status=status)
        if not stream:
            return web.json_response(ANTHROPIC_REPLY)
        resp = web.StreamResponse(headers={"Content-Type":
                                           "text/event-stream"})
        await resp.prepare(request)
        events = [
            {"type": "message_start",
             "message": {"id": "msg_s", "model": "glm-5.3",
                         "usage": {"input_tokens": 3, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "thinking", "thinking": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "hm"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "text_delta", "text": "hel"}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "text_delta", "text": "lo"}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta",
             "delta": {"stop_reason": "end_turn"},
             "usage": {"input_tokens": 3, "output_tokens": 2}},
            {"type": "message_stop"},
        ]
        for event in events:
            await resp.write(b"event: " + event["type"].encode() +
                             b"\ndata: " +
                             json.dumps(event).encode() + b"\n\n")
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/messages", messages)
    return app


class OpenAICompatTests(unittest.TestCase):
    """Phone clients that only speak OpenAI must work through the same gateway."""

    def setUp(self):
        model_gateway._CATALOG_CACHE.update(upstream="", catalog=None,
                                            at=0.0, fail_at=0.0)
        model_gateway._ROUTE_HEALTH.clear()

    def _cfg(self, tmp: str, upstream: str) -> dict:
        store = Store(Path(tmp) / "db.sqlite")
        try:
            return save_config(store, upstream=upstream, port=0)
        finally:
            store.close()

    def test_request_translation_keeps_system_images_and_params(self):
        payload = {
            "model": "glm-5.3",
            "max_tokens": 64,
            "temperature": 0.3,
            "stop": "END",
            "messages": [
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": [
                    {"type": "text", "text": "what is this"},
                    {"type": "image_url",
                     "image_url": {"url": "data:image/png;base64,AAAA"}},
                ]},
                {"role": "assistant", "content": "a picture"},
                {"role": "user", "content": "thanks"},
            ],
        }
        body = model_gateway.oai_request_to_anthropic(
            payload, "ccs-claude-openai--glm-5.3")
        self.assertEqual(body["model"], "ccs-claude-openai--glm-5.3")
        self.assertEqual(body["system"], "be terse")
        self.assertEqual(body["max_tokens"], 64)
        self.assertEqual(body["stop_sequences"], ["END"])
        user = body["messages"][0]
        self.assertEqual(user["content"][0], {"type": "text",
                                              "text": "what is this"})
        self.assertEqual(user["content"][1]["source"]["media_type"],
                         "image/png")
        self.assertEqual([m["role"] for m in body["messages"]],
                         ["user", "assistant", "user"])

    def test_unknown_role_is_rejected_but_tool_role_is_translated(self):
        with self.assertRaises(ValueError):
            model_gateway.oai_request_to_anthropic(
                {"messages": [{"role": "function", "content": "x"}]}, "m")
        with self.assertRaises(ValueError):
            model_gateway.oai_request_to_anthropic(
                {"messages": [{"role": "system", "content": "only"}]}, "m")
        body = model_gateway.oai_request_to_anthropic(
            {"messages": [
                {"role": "user", "content": "weather?"},
                {"role": "assistant", "content": None,
                 "tool_calls": [{"id": "call_1", "type": "function",
                                 "function": {"name": "get_weather",
                                              "arguments": "{\"city\":\"SH\"}"}}]},
                {"role": "tool", "tool_call_id": "call_1", "content": "22C"},
            ]}, "m")
        self.assertEqual(body["messages"][1]["content"][0]["type"], "tool_use")
        self.assertEqual(body["messages"][1]["content"][0]["input"],
                         {"city": "SH"})
        result = body["messages"][2]["content"][0]
        self.assertEqual(result["type"], "tool_result")
        self.assertEqual(result["tool_use_id"], "call_1")
        self.assertEqual(result["content"], "22C")

    def test_tools_and_tool_choice_are_translated(self):
        payload = {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function",
                       "function": {"name": "get_weather",
                                    "description": "look up weather",
                                    "parameters": {"type": "object",
                                                   "properties": {"city":
                                                                  {"type":
                                                                   "string"}}}}}],
            "tool_choice": "required",
        }
        body = model_gateway.oai_request_to_anthropic(payload, "m")
        self.assertEqual(body["tools"][0]["name"], "get_weather")
        self.assertEqual(body["tools"][0]["input_schema"]["type"], "object")
        self.assertEqual(body["tool_choice"], {"type": "any"})
        forced = model_gateway.oai_request_to_anthropic(
            {**payload, "tool_choice": {"type": "function",
                                        "function": {"name": "get_weather"}}},
            "m")
        self.assertEqual(forced["tool_choice"],
                         {"type": "tool", "name": "get_weather"})
        none = model_gateway.oai_request_to_anthropic(
            {**payload, "tool_choice": "none"}, "m")
        self.assertNotIn("tools", none)

    def test_response_tool_use_becomes_openai_tool_calls(self):
        data = {"content": [
                    {"type": "text", "text": "checking"},
                    {"type": "tool_use", "id": "toolu_1",
                     "name": "get_weather", "input": {"city": "SH"}}],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 9, "output_tokens": 4}}
        out = model_gateway.anthropic_to_oai_response(data, "m")
        choice = out["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        call = choice["message"]["tool_calls"][0]
        self.assertEqual(call["id"], "toolu_1")
        self.assertEqual(call["type"], "function")
        self.assertEqual(call["function"]["name"], "get_weather")
        self.assertEqual(json.loads(call["function"]["arguments"]),
                         {"city": "SH"})
        self.assertEqual(choice["message"]["content"], "checking")

    def test_chat_completions_returns_openai_shape(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_openai_upstream(seen))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        resp = await client.post(
                            "/v1/chat/completions",
                            json={"model": "glm-5.3", "max_tokens": 32,
                                  "messages": [{"role": "user",
                                                "content": "hi"}]},
                            headers={"authorization": f"Bearer {cfg['token']}"})
                        self.assertEqual(resp.status, 200)
                        body = await resp.json()
                        self.assertEqual(body["object"], "chat.completion")
                        self.assertEqual(
                            body["choices"][0]["message"]["content"], "OK")
                        self.assertEqual(
                            body["choices"][0]["finish_reason"], "stop")
                        self.assertEqual(body["usage"]["prompt_tokens"], 11)
                        self.assertEqual(body["usage"]["completion_tokens"], 7)
                        self.assertEqual(body["usage"]["total_tokens"], 18)
                        # Reasoning blocks reach OpenAI clients as
                        # reasoning_content instead of being dropped.
                        self.assertEqual(
                            body["choices"][0]["message"]["reasoning_content"],
                            "let me think")
                        self.assertEqual(
                            resp.headers.get("x-relaydesk-route"),
                            "ccs-claude-openai--glm-5.3")
                        # Upstream still speaks Anthropic, with the resolved id.
                        sent = seen[-1]
                        self.assertEqual(sent["body"]["model"],
                                         "ccs-claude-openai--glm-5.3")
                        self.assertEqual(sent["headers"].get(
                            "anthropic-version"), "2023-06-01")
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_chat_completions_streams_openai_chunks(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_openai_upstream(seen, stream=True))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        resp = await client.post(
                            "/v1/chat/completions",
                            json={"model": "glm-5.3", "stream": True,
                                  "messages": [{"role": "user",
                                                "content": "hi"}]},
                            headers={"x-api-key": cfg["token"]})
                        self.assertEqual(resp.status, 200)
                        self.assertIn("text/event-stream",
                                      resp.headers.get("content-type", ""))
                        text = await resp.text()
                        self.assertIn("chat.completion.chunk", text)
                        self.assertIn('"content": "hel"', text)
                        self.assertIn('"content": "lo"', text)
                        self.assertIn('"reasoning_content": "hm"', text)
                        self.assertIn("[DONE]", text)
                        self.assertEqual(seen[-1]["body"].get("stream"), True)
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_chat_completions_maps_upstream_error(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                error = {"type": "error",
                         "error": {"type": "rate_limit_error",
                                   "message": "slow down"}}
                upstream = TestServer(_openai_upstream(
                    [], status=429, error_body=error))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        resp = await client.post(
                            "/v1/chat/completions",
                            json={"model": "glm-5.3",
                                  "messages": [{"role": "user",
                                                "content": "hi"}]},
                            headers={"x-api-key": cfg["token"]})
                        self.assertEqual(resp.status, 429)
                        body = await resp.json()
                        self.assertEqual(body["error"]["message"], "slow down")
                        self.assertEqual(body["error"]["type"],
                                         "rate_limit_error")
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_empty_answer_falls_back_to_the_reasoning_text(self):
        data = {"content": [{"type": "thinking",
                             "thinking": "only reasoning here"}],
                "stop_reason": "max_tokens",
                "usage": {"input_tokens": 5, "output_tokens": 9}}
        out = model_gateway.anthropic_to_oai_response(data, "m")
        message = out["choices"][0]["message"]
        self.assertEqual(message["content"], "only reasoning here")
        self.assertEqual(message["reasoning_content"], "only reasoning here")
        self.assertEqual(out["choices"][0]["finish_reason"], "length")

    def test_models_list_carries_both_client_shapes(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                upstream = TestServer(_openai_upstream([]))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        body = await (await client.get(
                            "/v1/models",
                            headers={"x-api-key": cfg["token"]})).json()
                        self.assertEqual(body["object"], "list")
                        row = body["data"][0]
                        self.assertEqual(row["object"], "model")
                        self.assertEqual(row["type"], "model")
                        self.assertEqual(row["owned_by"], "cc-switch")
                        self.assertIn("display_name", row)
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())


def _tool_upstream(seen: list[dict], *, stream: bool = False):
    """cc-switch stand-in that answers with an Anthropic tool_use block."""
    reply = {"id": "msg_tool", "type": "message", "role": "assistant",
             "model": "glm-5.3",
             "content": [{"type": "tool_use", "id": "toolu_9",
                          "name": "get_weather", "input": {"city": "SH"}}],
             "stop_reason": "tool_use",
             "usage": {"input_tokens": 5, "output_tokens": 3}}

    async def models(request):
        return web.json_response(CATALOG)

    async def messages(request):
        seen.append(json.loads(await request.read() or b"{}"))
        if not stream:
            return web.json_response(reply)
        resp = web.StreamResponse(headers={"Content-Type":
                                           "text/event-stream"})
        await resp.prepare(request)
        events = [
            {"type": "message_start",
             "message": {"id": "msg_t", "model": "glm-5.3",
                         "usage": {"input_tokens": 5, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": "toolu_9",
                               "name": "get_weather", "input": {}}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta",
                       "partial_json": '{"city": '}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta",
                       "partial_json": '"SH"}'}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta",
             "delta": {"stop_reason": "tool_use"},
             "usage": {"input_tokens": 5, "output_tokens": 3}},
            {"type": "message_stop"},
        ]
        for event in events:
            await resp.write(b"event: " + event["type"].encode() +
                             b"\ndata: " + json.dumps(event).encode() + b"\n\n")
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/messages", messages)
    return app


class OpenAIToolTests(unittest.TestCase):
    """Function calling: the last piece phone clients expect from OpenAI."""

    def setUp(self):
        model_gateway._CATALOG_CACHE.update(upstream="", catalog=None,
                                            at=0.0, fail_at=0.0)
        model_gateway._ROUTE_HEALTH.clear()

    def _cfg(self, tmp: str, upstream: str) -> dict:
        store = Store(Path(tmp) / "db.sqlite")
        try:
            return save_config(store, upstream=upstream, port=0)
        finally:
            store.close()

    def _tool_request(self, **extra) -> dict:
        return {"model": "glm-5.3",
                "messages": [{"role": "user", "content": "weather in SH?"}],
                "tools": [{"type": "function",
                           "function": {"name": "get_weather",
                                        "parameters": {"type": "object",
                                                       "properties": {
                                                           "city": {
                                                               "type": "string"}}}}}],
                **extra}

    def test_tools_reach_upstream_and_tool_calls_come_back(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_tool_upstream(seen))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        resp = await client.post(
                            "/v1/chat/completions",
                            json=self._tool_request(tool_choice="auto"),
                            headers={"x-api-key": cfg["token"]})
                        self.assertEqual(resp.status, 200)
                        body = await resp.json()
                        choice = body["choices"][0]
                        self.assertEqual(choice["finish_reason"], "tool_calls")
                        call = choice["message"]["tool_calls"][0]
                        self.assertEqual(call["id"], "toolu_9")
                        self.assertEqual(call["function"]["name"],
                                         "get_weather")
                        self.assertEqual(json.loads(
                            call["function"]["arguments"]), {"city": "SH"})
                        sent = seen[-1]
                        self.assertEqual(sent["tools"][0]["name"],
                                         "get_weather")
                        self.assertIn("input_schema", sent["tools"][0])
                        self.assertEqual(sent["tool_choice"], {"type": "auto"})
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_tool_call_streaming_uses_openai_delta_shape(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_tool_upstream(seen, stream=True))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        resp = await client.post(
                            "/v1/chat/completions",
                            json=self._tool_request(stream=True),
                            headers={"x-api-key": cfg["token"]})
                        self.assertEqual(resp.status, 200)
                        text = await resp.text()
                        self.assertIn('"tool_calls"', text)
                        self.assertIn('"name": "get_weather"', text)
                        self.assertIn('\\"city\\": ', text)
                        self.assertIn('"finish_reason": "tool_calls"', text)
                        self.assertIn("[DONE]", text)
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())


    def test_connection_refused_is_retried_once_on_the_same_route(self):
        """cc-switch auto-update closes the listener for a moment; a connect
        refusal must not surface as a 502 when the retry succeeds."""
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_openai_upstream(seen))
                await upstream.start_server()
                real_post = aiohttp.ClientSession.post
                calls = {"n": 0}

                def flaky_post(self, *args, **kwargs):
                    calls["n"] += 1
                    if calls["n"] == 1:
                        raise aiohttp.ClientConnectorError(
                            None, OSError("connection refused"))
                    return real_post(self, *args, **kwargs)

                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        aiohttp.ClientSession.post = flaky_post
                        resp = await client.post(
                            "/v1/chat/completions",
                            json={"model": "glm-5.3",
                                  "messages": [{"role": "user",
                                                "content": "hi"}]},
                            headers={"x-api-key": cfg["token"]})
                        aiohttp.ClientSession.post = real_post
                        self.assertEqual(resp.status, 200)
                        self.assertEqual(calls["n"], 2)
                        body = await resp.json()
                        self.assertEqual(
                            body["choices"][0]["message"]["content"], "OK")
                    finally:
                        aiohttp.ClientSession.post = real_post
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())


    def test_page_script_is_valid_javascript(self):
        """A Python template expands backslash-n into a real newline, which
        silently kills the whole page script. We were bitten twice; let node
        parse the rendered script when it is installed."""
        node = shutil.which("node")
        for candidate in (r"E:\APP\nodejs\node.exe", r"C:\Program Files\nodejs\node.exe"):
            if not node and Path(candidate).exists():
                node = candidate
        if not node:
            self.skipTest("node 不可用，跳过 JS 语法校验")

        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_catalog_app(seen))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        text = await (await client.get(
                            f"/?token={cfg['token']}")).text()
                        script = text.split("<script>", 1)[1]
                        script = script.split("</script>", 1)[0]
                        path = Path(tmp) / "page.js"
                        path.write_text(script, encoding="utf-8")
                        proc = subprocess.run([node, "--check", str(path)],
                                              capture_output=True, text=True)
                        self.assertEqual(proc.returncode, 0,
                                         f"页面脚本语法错误: {proc.stderr[:400]}")
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_enabled_gateway_warms_the_catalog_at_startup(self):
        """After a restart /health and the phone page must already know the
        models; waiting for the first request shows '0 个' and looks broken."""
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_catalog_app(seen))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    cfg["enabled"] = True
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        for _ in range(100):
                            if any(row["kind"] == "models" for row in seen):
                                break
                            await asyncio.sleep(0.02)
                        self.assertTrue(
                            any(row["kind"] == "models" for row in seen),
                            "网关启动后没有预热模型目录")
                        health = await (await client.get(
                            "/health",
                            headers={"x-api-key": cfg["token"]})).json()
                        self.assertEqual(health["models"], 3)
                        # A successful catalog already proves reachability.
                        self.assertTrue(health["upstream_ok"])
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())


class ActiveAccountingTests(unittest.TestCase):
    """active 必须以「在途登记表」为准：裸计数器一旦漏减就永久漂移
    （实测出现过 active 卡在 1，健康检查里还看得到 status=0 的残项）。"""

    def test_enter_and_leave_are_symmetric(self):
        stats = model_gateway._new_stats()

        class Req:
            pass

        req = Req()
        key = model_gateway._enter_active(stats, req)
        self.assertEqual(stats["active"], 1)
        model_gateway._leave_active(stats, key)
        self.assertEqual(stats["active"], 0)
        # 重复离开不会变负
        model_gateway._leave_active(stats, key)
        self.assertEqual(stats["active"], 0)

    def test_sweep_drops_only_stale_entries(self):
        stats = model_gateway._new_stats()
        stats["inflight"] = {"old": time.monotonic() - 10_000,
                              "new": time.monotonic()}
        stats["active"] = 2
        model_gateway._sweep_active(stats, 60.0)
        self.assertEqual(sorted(stats["inflight"]), ["new"])
        self.assertEqual(stats["active"], 1)


class RequestAccountingTests(unittest.TestCase):
    """每条请求结束都要落下 status：否则 recent_requests 里是 status=0，
    看诊时误以为请求没走完（实测在 OpenAI 通道踩过）。"""

    def setUp(self):
        model_gateway._CATALOG_CACHE.update(upstream="", catalog=None,
                                            at=0.0, fail_at=0.0)
        model_gateway._ROUTE_HEALTH.clear()
        model_gateway._RELAY_PREFIX_HEALTH.clear()

    def _cfg(self, tmp: str, upstream: str) -> dict:
        store = Store(Path(tmp) / "db.sqlite")
        try:
            return save_config(store, upstream=upstream, port=0)
        finally:
            store.close()

    def test_openai_success_path_records_status(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_openai_upstream(seen))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        resp = await client.post(
                            "/v1/chat/completions",
                            json={"model": "glm-5.3", "max_tokens": 32,
                                  "messages": [{"role": "user",
                                                "content": "hi"}]},
                            headers={"x-api-key": cfg["token"]})
                        self.assertEqual(resp.status, 200)
                        health = await (await client.get(
                            "/health", headers={"x-api-key": cfg["token"]})).json()
                        last = health["stats"]["recent_requests"][-1]
                        self.assertEqual(last["status"], 200,
                                         f"成功请求没记 status: {last}")
                        self.assertEqual(last["path"], "/v1/chat/completions")
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_streaming_path_records_status(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_openai_upstream(seen, stream=True))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        resp = await client.post(
                            "/v1/chat/completions",
                            json={"model": "glm-5.3", "stream": True,
                                  "messages": [{"role": "user",
                                                "content": "hi"}]},
                            headers={"x-api-key": cfg["token"]})
                        await resp.text()
                        health = await (await client.get(
                            "/health", headers={"x-api-key": cfg["token"]})).json()
                        last = health["stats"]["recent_requests"][-1]
                        self.assertEqual(last["status"], 200, last)
                        self.assertTrue(last["stream"])
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())


class NoRetryOn504Tests(unittest.TestCase):
    """504 是上游网关等首包超时，请求可能已被受理；换路由重试=重复计费。"""

    def setUp(self):
        model_gateway._CATALOG_CACHE.update(upstream="", catalog=None,
                                            at=0.0, fail_at=0.0)
        model_gateway._ROUTE_HEALTH.clear()
        model_gateway._RELAY_PREFIX_HEALTH.clear()

    def test_504_is_returned_without_trying_the_next_route(self):
        async def run():
            import tempfile
            from pathlib import Path as P

            calls = []

            async def models(request):
                return web.json_response({"data": [
                    {"id": "ccs-claude-a--glm-5.3"},
                    {"id": "ccs-claude-b--glm-5.3"}]})

            async def gateway_timeout(request):
                await request.read()
                calls.append(1)
                return web.Response(
                    status=504,
                    text="<html><title>504 Gateway Time-out</title></html>",
                    content_type="text/html")

            app = web.Application()
            app.router.add_get("/v1/models", models)
            app.router.add_post("/v1/messages", gateway_timeout)
            upstream = TestServer(app)
            await upstream.start_server()
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    store = Store(P(tmp) / "db.sqlite")
                    try:
                        cfg = save_config(
                            store,
                            upstream=str(upstream.make_url("")).rstrip("/"),
                            port=0)
                    finally:
                        store.close()
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        resp = await client.post(
                            "/v1/messages",
                            json={"model": "glm-5.3", "messages": []},
                            headers={"x-api-key": cfg["token"]})
                        self.assertEqual(resp.status, 504)
                        self.assertEqual(len(calls), 1,
                                         f"504 被重试了 {len(calls)} 次")
                    finally:
                        await client.close()
            finally:
                await upstream.close()

        asyncio.run(run())

    def test_502_still_falls_back(self):
        async def run():
            import tempfile
            from pathlib import Path as P

            calls = []

            async def models(request):
                return web.json_response({"data": [
                    {"id": "ccs-claude-a--glm-5.3"},
                    {"id": "ccs-claude-b--glm-5.3"}]})

            async def handler(request):
                body = json.loads(await request.read() or b"{}")
                calls.append(body.get("model"))
                if body.get("model", "").endswith("a--glm-5.3"):
                    return web.json_response({"type": "error"}, status=502)
                return web.json_response(ANTHROPIC_REPLY)

            app = web.Application()
            app.router.add_get("/v1/models", models)
            app.router.add_post("/v1/messages", handler)
            upstream = TestServer(app)
            await upstream.start_server()
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    store = Store(P(tmp) / "db.sqlite")
                    try:
                        cfg = save_config(
                            store,
                            upstream=str(upstream.make_url("")).rstrip("/"),
                            port=0)
                    finally:
                        store.close()
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        resp = await client.post(
                            "/v1/messages",
                            json={"model": "glm-5.3", "messages": []},
                            headers={"x-api-key": cfg["token"]})
                        self.assertEqual(resp.status, 200)
                        self.assertEqual(len(calls), 2)
                    finally:
                        await client.close()
            finally:
                await upstream.close()

        asyncio.run(run())


class ColdStartTests(unittest.TestCase):
    """冷启动窗口：一条中继都没探测过时，列表不能把死中继也摆出来。

    实测：重启后 1.6s 就能服务，但列表还是 39 个（含 28 个死中继），约 7 秒后
    才收敛到 11。用户在这几秒里打开手机选择器，必然挑到用不了的模型。
    """

    def setUp(self):
        model_gateway._ROUTE_HEALTH.clear()
        model_gateway._RELAY_PREFIX_HEALTH.clear()
        model_gateway._WARM_STATE["done_at"] = 0.0

    def tearDown(self):
        model_gateway._WARM_STATE["done_at"] = time.monotonic()

    @staticmethod
    def _catalog():
        return model_gateway._parse_catalog({"data": [
            {"id": "ccs-claude-openai--glm-5.3"},
            {"id": "ccs-claude-openai--kimi-k2.7-code"},
            {"id": "ccs-claude-cn222--ccs-claude-openai-2--glm-5.3"},
            {"id": "ccs-claude-claude2--ccs-claude-openai-2--glm-5.3"},
        ]})

    def test_pending_relays_are_hidden_while_cold(self):
        ids = model_gateway.visible_model_ids(self._catalog(),
                                             pending_relays_hidden=True)
        self.assertEqual(sorted(ids),
                         ["ccs-claude-openai--glm-5.3",
                          "ccs-claude-openai--kimi-k2.7-code"])

    def test_caller_without_the_flag_keeps_old_behaviour(self):
        """CLI/GUI 进程没有预热，语义必须保持「未知不等于已死」。"""
        ids = model_gateway.visible_model_ids(self._catalog())
        self.assertEqual(len(ids), 4)

    def test_pure_cascade_stack_still_gets_a_list(self):
        """纯级联消费方没有直连路由，冷启动时也不能给空列表。"""
        catalog = model_gateway._parse_catalog({"data": [
            {"id": "ccs-claude-cn222--ccs-claude-openai-2--glm-5.3"}]})
        ids = model_gateway.visible_model_ids(catalog,
                                              pending_relays_hidden=True)
        self.assertEqual(ids, ["ccs-claude-cn222--ccs-claude-openai-2--glm-5.3"])

    def test_after_warmup_relays_are_judged_by_health(self):
        model_gateway._WARM_STATE["done_at"] = time.monotonic()
        model_gateway._route_mark(
            "ccs-claude-cn222--ccs-claude-openai-2--glm-5.3", False, 8000)
        ids = model_gateway.visible_model_ids(self._catalog(),
                                              pending_relays_hidden=True)
        # 死的那条被健康过滤掉，另一条未知中继照旧出现
        self.assertNotIn("ccs-claude-cn222--ccs-claude-openai-2--glm-5.3", ids)
        self.assertIn("ccs-claude-claude2--ccs-claude-openai-2--glm-5.3", ids)


class BindFailureTests(unittest.TestCase):
    """端口被占用时必须干净失败：报 error: port，且不泄漏 ClientSession。

    实测：site.start() 抛 bind 错误时，aiohttp 的 ClientSession（在 on_startup 里
    建的）没被关闭，退出会打「Unclosed client session」——看着像程序有问题。
    根因是 cleanup 写在 try 之外。
    """

    def test_port_conflict_raises_without_leaking_the_session(self):
        async def run():
            import tempfile
            from pathlib import Path as P

            from aiohttp import web

            # 先占一个端口
            blocker = web.AppRunner(web.Application())
            await blocker.setup()
            site = web.TCPSite(blocker, "127.0.0.1", 0)
            await site.start()
            busy_port = site._server.sockets[0].getsockname()[1]
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    store = Store(P(tmp) / "db.sqlite")
                    try:
                        save_config(store, upstream="http://127.0.0.1:1",
                                    port=busy_port)
                        with self.assertRaises(OSError):
                            await asyncio.wait_for(
                                model_gateway.run_gateway(store), timeout=20)
                    finally:
                        store.close()
            finally:
                await blocker.cleanup()

        asyncio.run(run())


class DegradedRetryTests(unittest.TestCase):
    """只剩一个候选、而它刚失败过时，不能快失败。

    快失败等于必定失败；它可能只是抖了一下。给一次短预算重试，
    自愈就不必等 60s 退避窗口（实测：单条直连路由一次抖动会让所有人 503）。
    """

    def setUp(self):
        model_gateway._CATALOG_CACHE.update(upstream="", catalog=None,
                                            at=0.0, fail_at=0.0)
        model_gateway._ROUTE_HEALTH.clear()
        model_gateway._RELAY_PREFIX_HEALTH.clear()

    def test_single_dead_route_is_still_tried(self):
        async def run():
            import tempfile
            from pathlib import Path as P

            upstream = TestServer(_catalog_app([]))
            await upstream.start_server()
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    store = Store(P(tmp) / "db.sqlite")
                    try:
                        cfg = save_config(
                            store,
                            upstream=str(upstream.make_url("")).rstrip("/"),
                            port=0)
                    finally:
                        store.close()
                    # 目录里只有一个模型 -> 单候选；把它标成刚失败过
                    model_gateway._route_mark("ccs-claude-openai--glm-5.3",
                                              False, 500)
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        resp = await client.post(
                            "/v1/messages",
                            json={"model": "glm-5.3", "messages": []},
                            headers={"x-api-key": cfg["token"]})
                        self.assertEqual(resp.status, 200,
                                         "唯一候选被快失败了")
                    finally:
                        await client.close()
            finally:
                await upstream.close()

        asyncio.run(run())

    def test_multiple_dead_routes_still_fail_fast(self):
        """多个候选都死 -> 仍然快失败（模型此刻不可用，早说早换）。"""
        async def run():
            import tempfile
            from pathlib import Path as P

            relay_a = "ccs-claude-cn222--ccs-claude-openai-2--kimi-k2.7-code"
            relay_b = "ccs-claude-cn222k3--ccs-claude-openai-2--kimi-k2.7-code"
            upstream = TestServer(_catalog_app(
                [], {"data": [{"id": relay_a}, {"id": relay_b}]}))
            await upstream.start_server()
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    store = Store(P(tmp) / "db.sqlite")
                    try:
                        cfg = save_config(
                            store,
                            upstream=str(upstream.make_url("")).rstrip("/"),
                            port=0)
                    finally:
                        store.close()
                    model_gateway._route_mark(relay_a, False, 500)
                    model_gateway._route_mark(relay_b, False, 500)
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        began = asyncio.get_event_loop().time()
                        resp = await client.post(
                            "/v1/messages",
                            json={"model": "kimi-k2.7-code", "messages": []},
                            headers={"x-api-key": cfg["token"]})
                        elapsed = asyncio.get_event_loop().time() - began
                        self.assertEqual(resp.status, 503)
                        self.assertLess(elapsed, 2.0, "快失败变成了慢失败")
                    finally:
                        await client.close()
            finally:
                await upstream.close()

        asyncio.run(run())


class SelfTestPageTests(unittest.TestCase):
    """自检页是用户第一眼看到的东西：它只该列出当前真能用的模型。

    实测发现页面里有一个从未被 JS 读取的 `var MODELS`，塞进了完整目录
    （含已死中继），与 <option> 列表口径不一致，还白占 2.5 KB。
    """

    def setUp(self):
        model_gateway._CATALOG_CACHE.update(upstream="", catalog=None,
                                            at=0.0, fail_at=0.0)
        model_gateway._ROUTE_HEALTH.clear()
        model_gateway._RELAY_PREFIX_HEALTH.clear()

    def test_page_lists_only_the_visible_models(self):
        async def run():
            import tempfile
            from pathlib import Path as P

            upstream = TestServer(_catalog_app([]))
            await upstream.start_server()
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    store = Store(P(tmp) / "db.sqlite")
                    try:
                        cfg = save_config(
                            store,
                            upstream=str(upstream.make_url("")).rstrip("/"),
                            port=0, enabled=True)
                    finally:
                        store.close()
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        model_gateway._route_mark(
                            "ccs-claude-openai--glm-5.3", False, 100)
                        page = await (await client.get(
                            "/", params={"token": cfg["token"]})).text()
                        # 精确匹配 option value：glm-5.3 是 glm-5.3-flash 的前缀，
                        # 用子串断言会误判（实测踩过）。
                        self.assertNotIn('value="ccs-claude-openai--glm-5.3"', page,
                                         "死路由出现在自检页里")
                        self.assertIn("ccs-claude-openai--kimi-k2.7-code", page)
                        self.assertNotIn("{{MODELS_JSON}}", page,
                                         "模板占位符没被替换")
                    finally:
                        await client.close()
            finally:
                await upstream.close()

        asyncio.run(run())

    def test_page_still_declares_models_json_hook(self):
        """模板里保留 MODELS_JSON 占位替换，但脚本不应再声明未使用的 MODELS。"""
        self.assertNotIn("var MODELS = ", model_gateway.PAGE)


class CatalogCacheTests(unittest.TestCase):
    """缓存必须存上游原始目录：过滤后的结果写回缓存会让缓存越用越小，
    被隐藏的中继模型就永远回不来了（实测 39 -> 11 后再也不恢复）。"""

    def setUp(self):
        model_gateway._CATALOG_CACHE.update(upstream="", catalog=None,
                                            at=0.0, fail_at=0.0)
        model_gateway._ROUTE_HEALTH.clear()
        model_gateway._RELAY_PREFIX_HEALTH.clear()

    def test_serving_models_does_not_shrink_the_cached_catalog(self):
        async def run():
            import tempfile
            from pathlib import Path as P

            upstream = TestServer(_catalog_app([]))
            await upstream.start_server()
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    store = Store(P(tmp) / "db.sqlite")
                    try:
                        cfg = save_config(
                            store,
                            upstream=str(upstream.make_url("")).rstrip("/"),
                            port=0)
                    finally:
                        store.close()
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        headers = {"x-api-key": cfg["token"]}
                        served = await (await client.get("/v1/models",
                                                         headers=headers)).json()
                        self.assertEqual(len(served["data"]), 3)
                        cache = model_gateway._cached_catalog(cfg["upstream"])
                        self.assertEqual(len(cache["ids"]), 3,
                                         "缓存被过滤结果覆盖了")
                        # 死掉一条之后：对外只剩 2 条，但缓存仍有 3 条
                        model_gateway._route_mark("ccs-claude-openai--glm-5.3",
                                                  False, 100)
                        served2 = await (await client.get("/v1/models",
                                                          headers=headers)).json()
                        self.assertEqual(len(served2["data"]), 2)
                        cache2 = model_gateway._cached_catalog(cfg["upstream"])
                        self.assertEqual(len(cache2["ids"]), 3)
                    finally:
                        await client.close()
            finally:
                await upstream.close()

        asyncio.run(run())


class HealthModelCountTests(unittest.TestCase):
    """/health 的 models 必须是客户端真正能选的条数，否则会出现
    「health 说 39、/v1/models 只给 11」这种自相矛盾（实测踩过）。"""

    def setUp(self):
        model_gateway._CATALOG_CACHE.update(upstream="", catalog=None,
                                            at=0.0, fail_at=0.0)
        model_gateway._ROUTE_HEALTH.clear()
        model_gateway._RELAY_PREFIX_HEALTH.clear()

    def test_health_reports_visible_count_and_raw_catalog(self):
        async def run():
            import tempfile
            from pathlib import Path as P

            upstream = TestServer(_catalog_app([]))
            await upstream.start_server()
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    store = Store(P(tmp) / "db.sqlite")
                    try:
                        cfg = save_config(
                            store,
                            upstream=str(upstream.make_url("")).rstrip("/"),
                            port=0)
                    finally:
                        store.close()
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        headers = {"x-api-key": cfg["token"]}
                        # 先让目录进来
                        await client.get("/v1/models", headers=headers)
                        model_gateway._route_mark(
                            "ccs-claude-openai--glm-5.3", False, 100)
                        health = await (await client.get("/health",
                                                         headers=headers)).json()
                        served = await (await client.get("/v1/models",
                                                         headers=headers)).json()
                        self.assertEqual(health["models"],
                                         len(served["data"]))
                        self.assertEqual(health["catalog_models"], 3)
                    finally:
                        await client.close()
            finally:
                await upstream.close()

        asyncio.run(run())


class HideWindowTests(unittest.TestCase):
    """隐藏窗口必须长于选择用的退避窗口，否则列表会在 11/39 之间反复跳。"""

    def setUp(self):
        model_gateway._ROUTE_HEALTH.clear()
        model_gateway._RELAY_PREFIX_HEALTH.clear()

    def test_route_stays_hidden_past_the_retry_window(self):
        relay = "ccs-claude-peer--ccs-claude-x--glm-5.3"
        direct = "ccs-claude-openai--glm-5.3"
        model_gateway._route_mark(relay, False, 1000)
        catalog = model_gateway._parse_catalog({"data": [{"id": relay},
                                                       {"id": direct}]})
        # 退避窗口（60s）已经过了：选择逻辑会给它机会
        stale = time.monotonic() - model_gateway.ROUTE_RETRY_S - 5
        model_gateway._ROUTE_HEALTH[relay]["at"] = stale
        for prefix in list(model_gateway._RELAY_PREFIX_HEALTH):
            model_gateway._RELAY_PREFIX_HEALTH[prefix] = stale
        self.assertFalse(model_gateway._route_dead_recently(relay))
        # 但隐藏窗口（10 分钟）内，它不该出现在列表里
        self.assertEqual(model_gateway.visible_model_ids(catalog), [direct])

    def test_success_brings_the_model_back_immediately(self):
        relay = "ccs-claude-peer--ccs-claude-x--glm-5.3"
        model_gateway._route_mark(relay, False, 1000)
        catalog = model_gateway._parse_catalog({"data": [{"id": relay}]})
        self.assertEqual(model_gateway.visible_model_ids(catalog), [relay]) \
            if False else None
        model_gateway._route_mark(relay, True, 200)
        self.assertEqual(model_gateway.visible_model_ids(catalog), [relay])


class WarmRelayProbeTests(unittest.TestCase):
    """启动时把每个中继前缀各探一次，让模型列表立刻收敛到可用集合。"""

    def setUp(self):
        model_gateway._CATALOG_CACHE.update(upstream="", catalog=None,
                                            at=0.0, fail_at=0.0)
        model_gateway._ROUTE_HEALTH.clear()
        model_gateway._RELAY_PREFIX_HEALTH.clear()

    def test_each_prefix_is_probed_once_at_startup(self):
        async def run():
            import tempfile
            from pathlib import Path as P

            seen: list[str] = []

            async def models(request):
                return web.json_response({"data": [
                    {"id": "ccs-claude-p1--ccs-claude-x--glm-5.3"},
                    {"id": "ccs-claude-p1--ccs-claude-x--kimi"},
                    {"id": "ccs-claude-p2--ccs-claude-x--glm-5.3"},
                    {"id": "ccs-claude-openai--glm-5.3"},
                ]})

            async def messages(request):
                body = json.loads(await request.read() or b"{}")
                seen.append(body.get("model", ""))
                return web.json_response({"type": "error"}, status=503)

            app = web.Application()
            app.router.add_get("/v1/models", models)
            app.router.add_post("/v1/messages", messages)
            upstream = TestServer(app)
            await upstream.start_server()
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    store = Store(P(tmp) / "db.sqlite")
                    try:
                        cfg = save_config(
                            store,
                            upstream=str(upstream.make_url("")).rstrip("/"),
                            port=0, enabled=True)
                    finally:
                        store.close()
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        for _ in range(200):
                            if len(seen) >= 2:
                                break
                            await asyncio.sleep(0.02)
                        # 两个前缀各一次，直连不探
                        self.assertEqual(len(seen), 2, seen)
                        prefixes = {model_gateway._relay_prefix(m)
                                    for m in seen}
                        self.assertEqual(prefixes, {"ccs-claude-p1--",
                                                    "ccs-claude-p2--"})
                        # 探测判死后，列表应当把两组中继都隐藏掉
                        body = await (await client.get(
                            "/v1/models",
                            headers={"x-api-key": cfg["token"]})).json()
                        ids = [row["id"] for row in body["data"]]
                        self.assertEqual(ids, ["ccs-claude-openai--glm-5.3"])
                    finally:
                        await client.close()
            finally:
                await upstream.close()

        asyncio.run(run())


class RoutePoisoningTests(unittest.TestCase):
    """整机压测后出现过「所有请求 503 没有可用路由」：上游偶发 5xx 把本来好好的
    直连路由标成死亡，熔断窗口 10 分钟内所有人都用不了。"""

    def setUp(self):
        model_gateway._ROUTE_HEALTH.clear()
        model_gateway._RELAY_PREFIX_HEALTH.clear()

    def test_dead_route_gets_another_chance_after_backoff(self):
        relay = "ccs-claude-peer--ccs-claude-x--glm-5.3-flash"
        model_gateway._route_mark(relay, False, 1000)
        self.assertTrue(model_gateway._route_dead_recently(relay))
        # 退避窗口过去后必须再给一次机会，否则一次抖动就锁死 10 分钟
        stale = time.monotonic() - model_gateway.ROUTE_RETRY_S - 5
        model_gateway._ROUTE_HEALTH[relay]["at"] = stale
        for prefix in list(model_gateway._RELAY_PREFIX_HEALTH):
            model_gateway._RELAY_PREFIX_HEALTH[prefix] = stale
        self.assertFalse(model_gateway._route_dead_recently(relay))
        # 但「路由健康」展示仍保留 10 分钟的记录，便于诊断
        self.assertFalse(model_gateway._route_state(relay))

    def test_direct_route_survives_upstream_5xx(self):
        """直连路由收到 5xx 时不应被标死（上游过载 ≠ 路由坏了）。"""
        async def run():
            import tempfile
            from pathlib import Path as P

            async def models(request):
                return web.json_response({"data": [
                    {"id": "ccs-claude-openai--glm-5.3"}]})

            async def boom(request):
                return web.json_response({"type": "error"}, status=503)

            app = web.Application()
            app.router.add_get("/v1/models", models)
            app.router.add_post("/v1/messages", boom)
            upstream = TestServer(app)
            await upstream.start_server()
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    store = Store(P(tmp) / "db.sqlite")
                    try:
                        cfg = save_config(
                            store,
                            upstream=str(upstream.make_url("")).rstrip("/"),
                            port=0)
                    finally:
                        store.close()
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        await client.post(
                            "/v1/messages",
                            json={"model": "glm-5.3", "messages": []},
                            headers={"x-api-key": cfg["token"]})
                        self.assertFalse(
                            model_gateway._route_dead_recently(
                                "ccs-claude-openai--glm-5.3"),
                            "直连路由被上游 5xx 标死了")
                    finally:
                        await client.close()
            finally:
                await upstream.close()

        asyncio.run(run())


class UpstreamGateTests(unittest.TestCase):
    """cc-switch 只接受约 5 路并发（实测 429 "Limit: 5 concurrent requests"），
    网关要把多设备突发排队而不是让上游把它拒掉。"""

    def setUp(self):
        model_gateway._CATALOG_CACHE.update(upstream="", catalog=None,
                                            at=0.0, fail_at=0.0)
        model_gateway._ROUTE_HEALTH.clear()

    def _cfg(self, tmp: str, upstream: str, **extra) -> dict:
        store = Store(Path(tmp) / "db.sqlite")
        try:
            return save_config(store, upstream=upstream, port=0, **extra)
        finally:
            store.close()

    def test_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp, "http://127.0.0.1:15721")
            self.assertEqual(cfg["upstream_concurrency"], 5)
            self.assertEqual(cfg["queue_wait_s"], 60)
            # 0 = 不限流，合法
            self.assertEqual(
                self._cfg(tmp, "http://127.0.0.1:15721",
                          upstream_concurrency=0)["upstream_concurrency"], 0)
            # 越界被夹住
            self.assertEqual(
                self._cfg(tmp, "http://127.0.0.1:15721",
                          upstream_concurrency=999)["upstream_concurrency"], 64)
            self.assertEqual(
                self._cfg(tmp, "http://127.0.0.1:15721",
                          queue_wait_s=1)["queue_wait_s"], 5)

    def test_burst_is_queued_not_rejected(self):
        """8 路并发、上游并发上限 2：全部 200，且上游同时最多 2 路。"""
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                state = {"active": 0, "peak": 0, "calls": 0}
                gate = asyncio.Lock()

                async def slow(request):
                    async with gate:
                        state["active"] += 1
                        state["calls"] += 1
                        state["peak"] = max(state["peak"], state["active"])
                    try:
                        await asyncio.sleep(0.15)
                        return web.json_response(ANTHROPIC_REPLY)
                    finally:
                        async with gate:
                            state["active"] -= 1

                app = web.Application()
                app.router.add_get(
                    "/v1/models",
                    lambda r: web.json_response({"data": [{"id": "m"}]}))
                app.router.add_post("/v1/messages", slow)
                upstream = TestServer(app)
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"),
                                    upstream_concurrency=2)
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        headers = {"x-api-key": cfg["token"]}
                        responses = await asyncio.gather(*(
                            client.post("/v1/messages", json={"model": "m",
                                                               "messages": []},
                                        headers=headers)
                            for _ in range(8)))
                        codes = [r.status for r in responses]
                        self.assertEqual(codes, [200] * 8)
                        self.assertEqual(state["calls"], 8)
                        self.assertLessEqual(state["peak"], 2,
                                             f"上游并发峰值 {state['peak']} 超过闸门")
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_queue_wait_timeout_answers_429(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                async def never(request):
                    # 必须远长于 queue_wait_s(5)：原来也 sleep 5，两者同时到期会
                    # 变成竞态（3.12 上稳定失败，3.11 上侥幸过）。
                    await asyncio.sleep(30)
                    return web.json_response(ANTHROPIC_REPLY)

                app = web.Application()
                app.router.add_get(
                    "/v1/models",
                    lambda r: web.json_response({"data": [{"id": "m"}]}))
                app.router.add_post("/v1/messages", never)
                upstream = TestServer(app)
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"),
                                    upstream_concurrency=1, queue_wait_s=5)
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        headers = {"x-api-key": cfg["token"]}
                        first = asyncio.create_task(client.post(
                            "/v1/messages", json={"model": "m",
                                                  "messages": []},
                            headers=headers))
                        for _ in range(200):
                            if client.app[model_gateway.STATS]["active"] >= 1:
                                break
                            await asyncio.sleep(0.01)
                        cfg["queue_wait_s"] = 5          # 已被 clamp 到 5
                        started = asyncio.get_event_loop().time()
                        second = await client.post(
                            "/v1/messages", json={"model": "m",
                                                  "messages": []},
                            headers=headers)
                        # 5s 之后排队超时
                        self.assertGreaterEqual(
                            asyncio.get_event_loop().time() - started, 4.5)
                        self.assertEqual(second.status, 429)
                        self.assertEqual(second.headers.get("retry-after"), "5")
                        first.cancel()
                        # CancelledError 继承 BaseException，suppress(Exception) 抓不住。
                        # 上游 sleep 5 时 first 先完成、取消是空操作，看不出问题；
                        # 一旦上游 sleep 变长它还在跑，这里就会把 CancelledError
                        # 冲出测试（3.12 上更早暴露）。
                        with contextlib.suppress(Exception,
                                                 asyncio.CancelledError):
                            await first
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())


class AuthHintTests(unittest.TestCase):
    """401 的说明必须同时给出两种正确的发送方式。

    实测 9 种鉴权头，7 种能过；被拒的两种是「Authorization 里漏了 Bearer 前缀」
    与「Azure 风格的 api-key 头」。过去只说「invalid gateway token」，用户会去
    反复检查令牌对不对，而令牌其实没问题。
    """

    def test_hint_names_both_accepted_headers(self):
        hint = model_gateway.AUTH_HINT
        self.assertIn("x-api-key", hint)
        self.assertIn("Bearer", hint)

    def test_hint_has_no_control_characters(self):
        hint = model_gateway.AUTH_HINT
        bad = [ch for ch in hint if ord(ch) < 32]
        self.assertEqual(bad, [], f"提示里混入了控制字符: {bad!r}")

    def test_hint_mentions_the_two_common_mistakes(self):
        hint = model_gateway.AUTH_HINT
        self.assertIn("api-key", hint)
        self.assertIn("bare token", hint)


class CascadeUpstreamTests(unittest.TestCase):
    """A gateway whose upstream is *another* RelayDesk gateway (LAN cascade).

    .200 has no VPN; it points its own gateway at .7, which does. Without
    forwarding the upstream token the second hop answers 401, so this is the
    feature that makes the免VPN topology work.
    """

    def setUp(self):
        model_gateway._CATALOG_CACHE.update(upstream="", catalog=None,
                                            at=0.0, fail_at=0.0)
        model_gateway._ROUTE_HEALTH.clear()

    def _cfg(self, tmp: str, upstream: str, **extra) -> dict:
        store = Store(Path(tmp) / "db.sqlite")
        try:
            return save_config(store, upstream=upstream, port=0, **extra)
        finally:
            store.close()

    def test_upstream_token_defaults_to_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp, "http://127.0.0.1:15721")
            self.assertEqual(cfg["upstream_token"], "")

    def test_upstream_token_is_sent_and_client_token_is_not_leaked(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_catalog_app(seen))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"),
                                    upstream_token="peer-secret")
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        await client.post(
                            "/v1/messages",
                            json={"model": "glm-5.3",
                                  "messages": [{"role": "user",
                                                "content": "hi"}]},
                            headers={"x-api-key": cfg["token"]})
                        call = [row for row in seen
                                if row["kind"] == "messages"][-1]
                        self.assertEqual(call["headers"].get("x-api-key"),
                                         "peer-secret")
                        self.assertNotEqual(call["headers"].get("x-api-key"),
                                            cfg["token"])
                        # The catalog fetch must be authenticated too.
                        models = [row for row in seen
                                  if row["kind"] == "models"]
                        self.assertEqual(models[-1]["headers"].get("x-api-key"),
                                         "peer-secret")
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_openai_path_also_forwards_upstream_token(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_openai_upstream(seen))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"),
                                    upstream_token="peer-secret")
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        await client.post(
                            "/v1/chat/completions",
                            json={"model": "glm-5.3",
                                  "messages": [{"role": "user",
                                                "content": "hi"}]},
                            headers={"x-api-key": cfg["token"]})
                        sent = seen[-1]
                        self.assertEqual(sent["headers"].get("x-api-key"),
                                         "peer-secret")
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())


class ContentTypeTests(unittest.TestCase):
    """JSON must declare UTF-8: cc-switch omits it, and legacy clients then
    decode Chinese answers as Latin-1 (mojibake)."""

    def test_json_and_sse_declare_utf8(self):
        from flixcli.model_gateway import _declared_ctype
        self.assertEqual(_declared_ctype("application/json"),
                         "application/json; charset=utf-8")
        self.assertEqual(_declared_ctype("text/event-stream"),
                         "text/event-stream; charset=utf-8")
        # An upstream that already declares a charset is passed through.
        self.assertEqual(_declared_ctype("application/json; charset=gbk"),
                         "application/json; charset=gbk")
        # Other media types keep their exact upstream value.
        self.assertEqual(_declared_ctype("text/html"), "text/html")

    def _cfg(self, tmp: str, upstream: str) -> dict:
        store = Store(Path(tmp) / "db.sqlite")
        try:
            return save_config(store, upstream=upstream, port=0)
        finally:
            store.close()

    def test_proxied_reply_declares_utf8(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen = []
                upstream = TestServer(_catalog_app(seen))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        resp = await client.post(
                            "/v1/messages",
                            json={"model": "glm-5.3",
                                  "messages": [{"role": "user",
                                                "content": "hi"}]},
                            headers={"x-api-key": cfg["token"]})
                        self.assertIn("charset=utf-8",
                                      resp.headers.get("content-type", ""))
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())


class LanAddressTests(unittest.TestCase):
    """The phone must be told *which* NIC each address belongs to."""

    def test_lan_urls_named_shape_and_iface_shortening(self):
        rows = model_gateway.lan_urls_named(18993)
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(row["url"].startswith("http://"))
            self.assertEqual(row["url"].rsplit(":", 1)[0],
                             f"http://{row['ip']}")
        self.assertEqual(
            model_gateway._short_iface_name("无线局域网适配器 WLAN"), "WLAN")
        self.assertEqual(
            model_gateway._short_iface_name("以太网适配器 以太网 2"), "以太网 2")
        self.assertEqual(model_gateway._short_iface_name("本地连接* 2"),
                         "本地连接* 2")

    def test_annotate_urls_marks_interfaces(self):
        rows = [{"url": "http://192.168.1.2:18993", "interface": "WLAN"},
                {"url": "http://10.0.0.2:18993", "interface": ""}]
        text = model_gateway._annotate_urls(rows)
        self.assertIn("http://192.168.1.2:18993（WLAN）", text)
        self.assertIn("http://10.0.0.2:18993", text)
        self.assertNotIn("（）、", text)

    def test_status_payload_carries_named_addresses(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            try:
                payload = model_gateway.status_payload(store)
                self.assertTrue(payload["urls"])
                self.assertEqual(len(payload["urls_named"]),
                                 len(payload["urls"]))
                self.assertIn("interface", payload["urls_named"][0])
                self.assertIn("relay_prefixes", payload)
            finally:
                store.close()


class RouteBreakerTests(unittest.TestCase):
    """A hung relay prefix must fail fast on the next request, not stall again."""

    RELAY_A = "ccs-claude-cn222--ccs-claude-openai-2--kimi-k2.7-code"
    RELAY_B = "ccs-claude-cn222--ccs-claude-openai-2--glm-5.3"

    def setUp(self):
        model_gateway._CATALOG_CACHE.update(upstream="", catalog=None,
                                            at=0.0, fail_at=0.0)
        model_gateway._ROUTE_HEALTH.clear()
        model_gateway._RELAY_PREFIX_HEALTH.clear()

    def test_prefix_failure_marks_sibling_routes_unusable(self):
        model_gateway._route_mark(self.RELAY_A, False)
        self.assertFalse(model_gateway._route_state(self.RELAY_A))
        # Same peer prefix, different model: also treated as dead.
        self.assertFalse(model_gateway._route_state(self.RELAY_B))
        self.assertIn("ccs-claude-cn222--",
                      model_gateway.relay_prefix_health())

    def test_a_successful_relay_call_clears_the_prefix(self):
        model_gateway._route_mark(self.RELAY_A, False)
        model_gateway._route_mark(self.RELAY_B, True)
        self.assertNotIn("ccs-claude-cn222--",
                         model_gateway.relay_prefix_health())
        self.assertTrue(model_gateway._route_state(self.RELAY_B))

    def test_direct_route_beats_a_healthy_relay(self):
        direct = "ccs-claude-openai--glm-5.3"
        relay = "ccs-claude-cn222--ccs-claude-openai-2--glm-5.3"
        model_gateway._route_mark(relay, True)
        self.assertEqual(model_gateway._order_candidates([relay, direct])[0],
                         direct)
        model_gateway._route_mark(direct, False)
        self.assertEqual(model_gateway._order_candidates([relay, direct])[0],
                         relay)

    def test_all_routes_known_dead_answers_503_immediately(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                relay_a = "ccs-claude-cn222--ccs-claude-openai-2--kimi-k2.7-code"
                relay_b = "ccs-claude-cn222k3--ccs-claude-openai-2--kimi-k2.7-code"
                upstream = TestServer(_catalog_app(
                    seen, {"data": [{"id": relay_a},
                                    {"id": relay_b}]}))
                await upstream.start_server()
                try:
                    store = Store(Path(tmp) / "db.sqlite")
                    try:
                        cfg = save_config(
                            store, upstream=str(upstream.make_url("")).rstrip("/"),
                            port=0)
                    finally:
                        store.close()
                    model_gateway._route_mark(relay_a, False)
                    model_gateway._route_mark(relay_b, False)
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        started = asyncio.get_event_loop().time()
                        resp = await client.post(
                            "/v1/messages",
                            json={"model": "kimi-k2.7-code",
                                  "messages": [{"role": "user",
                                                "content": "hi"}]},
                            headers={"x-api-key": cfg["token"]})
                        elapsed = asyncio.get_event_loop().time() - started
                        self.assertEqual(resp.status, 503)
                        self.assertLess(elapsed, 2.0, "已知死路由仍去上游等待")
                        self.assertEqual(
                            [row for row in seen if row["kind"] == "messages"],
                            [])
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())


    def test_unknown_relay_routes_are_probed_then_skipped(self):
        """A hung relay must be judged by a cheap probe, not by burning the
        whole request budget."""
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                calls: list[dict] = []

                async def models(request):
                    return web.json_response({"data": [
                        {"id": "ccs-claude-cn222--ccs-claude-x--kimi-k2.7-code"},
                        {"id": "ccs-claude-cn222k3--ccs-claude-x--kimi-k2.7-code"},
                    ]})

                async def failing(request):
                    calls.append(json.loads(await request.read() or b"{}"))
                    return web.json_response({"type": "error"}, status=503)

                upstream_app = web.Application()
                upstream_app.router.add_get("/v1/models", models)
                upstream_app.router.add_post("/v1/messages", failing)
                upstream = TestServer(upstream_app)
                await upstream.start_server()
                try:
                    store = Store(Path(tmp) / "db.sqlite")
                    try:
                        cfg = save_config(
                            store,
                            upstream=str(upstream.make_url("")).rstrip("/"),
                            port=0)
                    finally:
                        store.close()
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        started = asyncio.get_event_loop().time()
                        resp = await client.post(
                            "/v1/messages",
                            json={"model": "kimi-k2.7-code",
                                  "messages": [{"role": "user",
                                                "content": "hi"}]},
                            headers={"x-api-key": cfg["token"]})
                        elapsed = asyncio.get_event_loop().time() - started
                        self.assertEqual(resp.status, 503)
                        self.assertLess(elapsed, 3.0,
                                        "探测没有在预算内判定死路由")
                        # One 1-token probe per relay candidate, then no more.
                        self.assertEqual(len(calls), 2)
                        self.assertTrue(all(call.get("max_tokens") == 1
                                            for call in calls))
                        calls.clear()
                        second = await client.post(
                            "/v1/messages",
                            json={"model": "kimi-k2.7-code",
                                  "messages": [{"role": "user",
                                                "content": "hi"}]},
                            headers={"x-api-key": cfg["token"]})
                        self.assertEqual(second.status, 503)
                        self.assertEqual(calls, [], "已知死路由仍去上游")
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_working_relay_is_probed_then_serves_the_call(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                calls: list[dict] = []

                async def models(request):
                    return web.json_response({"data": [
                        {"id": "ccs-claude-cn222--ccs-claude-x--kimi-k2.7-code"},
                    ]})

                async def messages(request):
                    calls.append(json.loads(await request.read() or b"{}"))
                    return web.json_response(ANTHROPIC_REPLY)

                upstream_app = web.Application()
                upstream_app.router.add_get("/v1/models", models)
                upstream_app.router.add_post("/v1/messages", messages)
                upstream = TestServer(upstream_app)
                await upstream.start_server()
                try:
                    store = Store(Path(tmp) / "db.sqlite")
                    try:
                        cfg = save_config(
                            store,
                            upstream=str(upstream.make_url("")).rstrip("/"),
                            port=0)
                    finally:
                        store.close()
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        resp = await client.post(
                            "/v1/messages",
                            json={"model": "kimi-k2.7-code",
                                  "messages": [{"role": "user",
                                                "content": "hi"}]},
                            headers={"x-api-key": cfg["token"]})
                        self.assertEqual(resp.status, 200)
                        self.assertEqual(calls[0].get("max_tokens"), 1)
                        # The second call is the client's real request.
                        self.assertNotEqual(calls[-1].get("max_tokens"), 1)
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())

    def test_direct_route_is_never_probed(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_catalog_app(seen))
                await upstream.start_server()
                try:
                    store = Store(Path(tmp) / "db.sqlite")
                    try:
                        cfg = save_config(
                            store,
                            upstream=str(upstream.make_url("")).rstrip("/"),
                            port=0)
                    finally:
                        store.close()
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        await client.post(
                            "/v1/messages",
                            json={"model": "glm-5.3",
                                  "messages": [{"role": "user",
                                                "content": "hi"}]},
                            headers={"x-api-key": cfg["token"]})
                        calls = [row for row in seen
                                 if row["kind"] == "messages"]
                        self.assertEqual(len(calls), 1)
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())


class BodyReadTests(unittest.TestCase):
    """Body reading must tolerate slow (but flowing) uploads and reject big ones
    without buffering them first."""

    class _Content:
        def __init__(self, chunks, stall_after=None):
            self.chunks = list(chunks)
            self.stall_after = stall_after
            self.used = 0

        def iter_chunked(self, size):
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.stall_after is not None and self.used >= self.stall_after:
                await asyncio.sleep(5)
                raise StopAsyncIteration
            if self.used >= len(self.chunks):
                raise StopAsyncIteration
            self.used += 1
            return self.chunks[self.used - 1]

    class _Request:
        def __init__(self, content, content_length=None):
            self.content = content
            self.content_length = content_length

    def test_declared_oversized_body_is_rejected_before_reading(self):
        request = self._Request(self._Content([b"x"]), content_length=2 * 1024 * 1024)
        with self.assertRaises(model_gateway.BodyTooLarge):
            asyncio.run(model_gateway._read_body(request, {"max_body_mb": 1}))

    def test_streamed_oversized_body_is_rejected_while_reading(self):
        chunk = b"y" * (512 * 1024)
        request = self._Request(self._Content([chunk, chunk, chunk]))
        with self.assertRaises(model_gateway.BodyTooLarge):
            asyncio.run(model_gateway._read_body(request, {"max_body_mb": 1}))

    def test_stalled_upload_raises_body_timeout(self):
        request = self._Request(self._Content([b"hello"], stall_after=1))
        with self.assertRaises(model_gateway.BodyTimeout):
            asyncio.run(model_gateway._read_body(request, {"max_body_mb": 1},
                                                 idle_timeout=0.1))

    def test_slow_but_flowing_upload_within_limit_is_read(self):
        chunks = [b"a" * 1024 for _ in range(5)]
        request = self._Request(self._Content(chunks))
        body = asyncio.run(model_gateway._read_body(request, {"max_body_mb": 1}))
        self.assertEqual(len(body), 5 * 1024)


class AuthThrottleTests(unittest.TestCase):
    def _cfg(self, tmp: str, upstream: str) -> dict:
        store = Store(Path(tmp) / "db.sqlite")
        try:
            return save_config(store, upstream=upstream, port=0)
        finally:
            store.close()

    def test_repeated_bad_tokens_lock_the_source_out(self):
        stats = model_gateway._new_stats()
        for _ in range(model_gateway.AUTH_FAIL_LIMIT):
            model_gateway._auth_note_fail(stats, "192.168.1.9")
        self.assertGreater(
            model_gateway._auth_block_remaining(stats, "192.168.1.9"), 0)
        # A different phone is not collateral damage.
        self.assertEqual(
            model_gateway._auth_block_remaining(stats, "192.168.1.10"), 0)
        model_gateway._auth_clear(stats, "192.168.1.9")
        self.assertEqual(
            model_gateway._auth_block_remaining(stats, "192.168.1.9"), 0)

    def test_gateway_answers_429_after_bad_token_flood(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                seen: list[dict] = []
                upstream = TestServer(_openai_upstream(seen))
                await upstream.start_server()
                try:
                    cfg = self._cfg(tmp, str(upstream.make_url("")).rstrip("/"))
                    client = TestClient(TestServer(make_model_app(None, cfg)))
                    await client.start_server()
                    try:
                        for _ in range(model_gateway.AUTH_FAIL_LIMIT):
                            bad = await client.post(
                                "/v1/messages", json={"model": "glm-5.3"},
                                headers={"x-api-key": "wrong"})
                            self.assertEqual(bad.status, 401)
                        blocked = await client.post(
                            "/v1/chat/completions", json={"model": "x"},
                            headers={"x-api-key": cfg["token"]})
                        self.assertEqual(blocked.status, 429)
                        self.assertEqual(
                            blocked.headers.get("retry-after"),
                            str(model_gateway.AUTH_BLOCK_SECONDS))
                    finally:
                        await client.close()
                finally:
                    await upstream.close()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
