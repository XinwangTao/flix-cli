"""Web uploads must honour the same size cap as peer file transfers.

A PIN-authenticated visitor could otherwise stream an unbounded body onto the
daemon's disk. ``max_receive_bytes`` already caps incoming peer files; apply it
per uploaded file as well.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from flixcli.config import Store
from flixcli.webapp import make_web_app

ME = {"fingerprint": "self-fp", "alias": "me", "deviceModel": "Windows"}
LIMIT = 1024


class WebUploadLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "case"
        self.root.mkdir()
        self.store = Store(self.root / "db.sqlite")
        self.store.set_pref("saveDir", str(self.root / "downloads"))
        self.store.set_pref("max_receive_bytes", str(LIMIT))
        app, _push = make_web_app(
            self.store, ME, lambda *_: None,
            lambda: str(self.root / "downloads"), 8891)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.pin = self.store.own_web_pin()
        self.downloads = self.root / "downloads"

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self._tmp.cleanup()

    async def _upload(self, files: list[tuple[str, bytes]]):
        form = FormData()
        for name, blob in files:
            form.add_field("file", blob, filename=name,
                           content_type="application/octet-stream")
        return await self.client.post(f"/upload?pin={self.pin}", data=form)

    async def test_oversized_file_is_rejected_and_leaves_nothing(self):
        r = await self._upload([("big.bin", b"x" * (LIMIT + 1))])
        self.assertEqual(r.status, 413)
        body = await r.json()
        self.assertEqual(body["error"], "too-large")
        self.assertEqual(body["limit"], LIMIT)
        self.assertEqual(list(self.downloads.iterdir()), [])

    async def test_exact_limit_is_accepted(self):
        blob = b"y" * LIMIT
        r = await self._upload([("edge.bin", blob)])
        self.assertEqual(r.status, 200)
        self.assertEqual((self.downloads / "edge.bin").read_bytes(), blob)

    async def test_limit_is_per_file_not_per_request(self):
        r = await self._upload([("a.bin", b"a" * LIMIT), ("b.bin", b"b" * LIMIT)])
        self.assertEqual(r.status, 200)
        self.assertEqual((self.downloads / "a.bin").read_bytes(), b"a" * LIMIT)
        self.assertEqual((self.downloads / "b.bin").read_bytes(), b"b" * LIMIT)


if __name__ == "__main__":
    unittest.main()
