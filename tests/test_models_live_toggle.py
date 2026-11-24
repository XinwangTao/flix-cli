"""Model gateway on/off without restarting the daemon.

`flix models stop` only flipped the pref: the daemon kept listening on 18993 and
kept serving requests, so a "closed" share was still answering the whole LAN.
`GatewayHost` owns the listener; the desktop API toggles it live.
"""
from __future__ import annotations

import asyncio
import socket
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from flixcli import model_gateway
from flixcli.config import Store
from flixcli.desktop_api import make_desktop_app
from flixcli.model_gateway import (GatewayHost, load_config, make_model_app,
                                   save_config)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_open(port: int, timeout: float = 0.4) -> bool:
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", port)) == 0


class GatewayHostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "db.sqlite")
        self.port = _free_port()
        save_config(self.store, enabled=True, port=self.port,
                    upstream="http://127.0.0.1:9")
        self.host = GatewayHost(self.store)

    async def asyncTearDown(self):
        await self.host.stop()
        self.store.close()
        self._tmp.cleanup()

    async def test_start_opens_the_port_and_stop_closes_it(self):
        info = await self.host.start(self.port)
        self.assertTrue(info["ok"])
        self.assertTrue(self.host.listening)
        self.assertTrue(_port_open(self.port), "start 之后端口应可连")

        stopped = await self.host.stop()
        self.assertFalse(stopped["listening"])
        self.assertFalse(self.host.listening)
        self.assertFalse(_port_open(self.port), "stop 之后端口必须真的关掉")

    async def test_toggle_can_be_repeated(self):
        for _ in range(3):
            await self.host.start(self.port)
            self.assertTrue(_port_open(self.port))
            await self.host.stop()
            self.assertFalse(_port_open(self.port))

    async def test_start_while_listening_is_idempotent(self):
        await self.host.start(self.port)
        again = await self.host.start(self.port)
        self.assertTrue(again.get("already"), again)
        self.assertTrue(self.host.listening)

    async def test_failed_start_keeps_the_host_clean(self):
        """A bind that fails must not leave a half-built runner behind.

        Windows' SO_REUSEADDR makes a real port clash unreliable to reproduce,
        so the failure is injected at the TCPSite that actually binds.
        """
        from unittest import mock

        from aiohttp import web

        real_site = web.TCPSite

        class Boom(real_site):  # type: ignore[misc, valid-type]
            async def start(self) -> None:
                raise OSError("address already in use")

        with mock.patch.object(model_gateway.web, "TCPSite", Boom):
            with self.assertRaises(OSError):
                await self.host.start(self.port)
        self.assertFalse(self.host.listening,
                         "启动失败后不能留下半初始化的 runner")
        # The same host object must recover once binding works again.
        await self.host.start(self.port)
        self.assertTrue(self.host.listening)
        self.assertTrue(_port_open(self.port))


class ModelsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "db.sqlite")
        self.port = _free_port()
        save_config(self.store, enabled=False, port=self.port,
                    upstream="http://127.0.0.1:9")
        self.host = GatewayHost(self.store)
        app, _ = make_desktop_app(
            self.store, {"fingerprint": "fp-me", "alias": "me"},
            lambda to, text: None, api_port=8898, model_host=self.host)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.host.stop()
        self.store.close()
        self._tmp.cleanup()

    async def test_disable_really_stops_listening(self):
        enabled = await self.client.post("/api/models/enable", json={})
        self.assertEqual(enabled.status, 200)
        body = await enabled.json()
        self.assertTrue(body["listening"])
        self.assertTrue(_port_open(self.port))

        disabled = await self.client.post("/api/models/disable", json={})
        self.assertEqual(disabled.status, 200)
        body = await disabled.json()
        self.assertFalse(body["listening"])
        self.assertFalse(body["enabled"])
        self.assertFalse(_port_open(self.port), "关闭共享后端口必须立刻关闭")
        self.assertFalse(load_config(self.store)["enabled"])

    async def test_status_reflects_the_live_state(self):
        await self.client.post("/api/models/enable", json={"port": self.port})
        status = await (await self.client.get("/api/models/status")).json()
        self.assertTrue(status["listening"])
        self.assertEqual(status["port"], self.port)
        self.assertTrue(status["token"].startswith("rd-"))

        await self.client.post("/api/models/disable", json={})
        status = await (await self.client.get("/api/models/status")).json()
        self.assertFalse(status["listening"])

    async def test_api_without_a_host_is_unavailable(self):
        app, _ = make_desktop_app(
            self.store, {"fingerprint": "fp-me", "alias": "me"},
            lambda to, text: None, api_port=8898, model_host=None)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post("/api/models/enable", json={})
            self.assertEqual(resp.status, 503)
            self.assertEqual((await resp.json())["error"], "models-unavailable")
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
