"""LAN model gateway: token gate, verbatim proxying, SSE passthrough."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from flixcli.config import Store
from flixcli.model_gateway import (CONFIG_TEMPLATE, load_config, make_model_app,
                                   new_token, reset_token, save_config)


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
                            f"/v1/messages?stream=1&token={cfg['token']}",
                            json={"model": "claude-haiku-4-5"},
                            headers={"accept": "text/event-stream"})
                        self.assertEqual(resp.status, 200)
                        text = await resp.text()
                        self.assertIn("event: message_start", text)
                        self.assertIn("event: message_stop", text)
                        self.assertEqual(seen[0]["query"].get("stream"), "1")
                        self.assertNotIn("token", seen[0]["query"])
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
                        health = await (await client.get("/health")).json()
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
                self.assertEqual(save_config(store, upstream="http://x:1/")[
                    "upstream"], "http://x:1")
            finally:
                store.close()

    def test_new_token_prefix(self):
        self.assertTrue(new_token().startswith("rd-"))


if __name__ == "__main__":
    unittest.main()
