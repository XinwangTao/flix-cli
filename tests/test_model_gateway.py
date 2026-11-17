"""LAN model gateway: token gate, verbatim proxying, SSE passthrough."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from flixcli.config import Store
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
                        await asyncio.sleep(0.2)  # let it reach the upstream
                        second = await client.post(
                            "/v1/messages", json={"model": "x"}, headers=headers)
                        self.assertEqual(second.status, 429)
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

    def test_concurrency_default_is_clamped(self):
        self.assertEqual(CONFIG_TEMPLATE["max_concurrent"], 4)
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            try:
                cfg = save_config(store, max_concurrent=999)
                self.assertEqual(cfg["max_concurrent"], 32)
                cfg = save_config(store, max_concurrent=0)
                self.assertEqual(cfg["max_concurrent"], 1)
            finally:
                store.close()

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
                    resp = await client.post("/v1/messages", json={},
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
                            "/v1/messages", json={},
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
                            "/v1/messages", json={},
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


if __name__ == "__main__":
    unittest.main()
