"""Health probe payload used by deploy checks and monitoring."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from flixcli.config import Store
from flixcli.webapp import health_payload, make_web_app

ME = {"fingerprint": "self-fp", "alias": "me", "deviceModel": "Windows"}


class HealthPayloadTests(unittest.TestCase):
    IDENT = {"alias": "board", "fingerprint": "abcd1234", "version": 1}

    def test_reports_identity_and_uptime(self):
        p = health_payload(self.IDENT, started_at=100.0, now=160.9,
                           device_count=3, channel_port=8891)
        self.assertTrue(p["ok"])
        self.assertEqual(p["alias"], "board")
        self.assertEqual(p["fingerprint"], "abcd1234")
        self.assertEqual(p["channelPort"], 8891)
        self.assertEqual(p["devices"], 3)
        self.assertEqual(p["uptime"], 60)

    def test_never_reports_negative_uptime(self):
        p = health_payload(self.IDENT, started_at=200.0, now=100.0,
                           device_count=0, channel_port=8891)
        self.assertEqual(p["uptime"], 0)

    def test_missing_identity_fields_default(self):
        p = health_payload({}, started_at=0.0, now=5.0, device_count=0,
                           channel_port=1)
        self.assertEqual(p["alias"], "")
        self.assertEqual(p["fingerprint"], "")


class HealthRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "case"
        self.root.mkdir()
        self.store = Store(self.root / "db.sqlite")
        self.store.set_pref("saveDir", str(self.root / "downloads"))

        def bad_update():
            raise RuntimeError("boom")

        app, _push = make_web_app(
            self.store, ME, lambda *_: None,
            lambda: str(self.root / "downloads"), 8891,
            update_info=bad_update)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self._tmp.cleanup()

    async def test_update_info_exception_keeps_the_probe_200(self):
        r = await self.client.get("/healthz")
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["update"],
                         {"ok": False, "error": "update-info-failed"})

    async def test_clipboard_describe_exception_keeps_the_probe_200(self):
        with mock.patch("flixcli.webapp.clipboard_mod.describe",
                        side_effect=RuntimeError("boom")):
            r = await self.client.get("/healthz")
        self.assertEqual(r.status, 200)
        self.assertEqual((await r.json())["clipboard"], "unknown")


if __name__ == "__main__":
    unittest.main()
