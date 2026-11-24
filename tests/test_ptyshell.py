"""Interactive PTY (rdShell phase 2): session, trust gate, WebSocket wiring."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from flixcli import ptyshell, server
from flixcli.config import Store


@unittest.skipUnless(ptyshell.available(), "PTY backend not installed")
class PtySessionTests(unittest.TestCase):
    def test_echo_roundtrip(self):
        session = ptyshell.PtySession("", 80, 24)
        try:
            deadline = time.monotonic() + 15
            out = b""
            while time.monotonic() < deadline and b"pty-ok" not in out:
                session.write(b"echo pty-ok\r")
                out += session.read(4096)
                time.sleep(0.2)
            self.assertIn(b"pty-ok", out)
        finally:
            session.close()

    def test_resize_updates_dimensions(self):
        session = ptyshell.PtySession("", 80, 24)
        try:
            session.resize(120, 40)
            self.assertEqual((session.cols, session.rows), (120, 40))
        finally:
            session.close()


class PtyTrustTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "relaydesk.db")
        self.store.add_pair("fp-peer", "1234")
        self.store.upsert_device("fp-peer", alias="peer", ip="127.0.0.1", port=8891)
        app = server.make_app(self.store, {"fingerprint": "fp-me", "alias": "me"},
                              lambda name, payload: None)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self._tmp.cleanup()

    async def test_unpaired_sender_is_refused(self):
        resp = await self.client.get("/shell/pty?from=fp-stranger")
        self.assertEqual(resp.status, 403)

    async def test_source_ip_mismatch_is_refused(self):
        self.store.upsert_device("fp-other", alias="other", ip="10.9.9.9", port=8891)
        self.store.add_pair("fp-other", "1234")
        resp = await self.client.get("/shell/pty?from=fp-other")
        self.assertEqual(resp.status, 403)

    @unittest.skipUnless(ptyshell.available(), "PTY backend not installed")
    async def test_paired_peer_gets_a_session(self):
        ws = await self.client.ws_connect("/shell/pty?from=fp-peer&sid=test-1")
        try:
            buf = b""
            deadline = time.monotonic() + 20
            await ws.send_json({"t": "data",
                                "b": base64.b64encode(b"echo ws-ok\r").decode()})
            while time.monotonic() < deadline and b"ws-ok" not in buf:
                msg = await asyncio.wait_for(ws.receive(), timeout=5)
                if msg.type.name != "TEXT":
                    continue
                frame = json.loads(msg.data)
                if frame.get("t") == "data":
                    buf += base64.b64decode(frame.get("b") or "")
                elif frame.get("t") == "error":
                    self.skipTest(f"PTY unavailable: {frame.get('detail')}")
            self.assertIn(b"ws-ok", buf)
        finally:
            await ws.close()
            await asyncio.sleep(0.2)
        audit = self.store.shell_audit()
        self.assertTrue(any(row["id"] == "test-1" for row in audit))

    @unittest.skipUnless(ptyshell.available(), "PTY backend not installed")
    async def test_slot_is_released_when_teardown_raises(self):
        """One raising teardown must not poison the only session slot.

        Seen in the field: `session.close()` (pywinpty on an already dead child)
        raised inside `finally` *before* the decrement, so the peer answered 409
        "busy" to every later attach until its daemon was restarted by hand.
        """
        real_close = ptyshell.PtySession.close
        calls = {"n": 0}

        def flaky_close(session):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("pywinpty closed twice")
            real_close(session)

        with mock.patch.object(ptyshell.PtySession, "close", flaky_close):
            ws = await self.client.ws_connect("/shell/pty?from=fp-peer&sid=leak-1")
            await ws.close()
            await asyncio.sleep(1.0)

        try:
            ws2 = await self.client.ws_connect("/shell/pty?from=fp-peer&sid=leak-2")
        except Exception as exc:  # noqa: BLE001 - assert with a useful message
            self.fail(f"session slot was not released after a failed teardown: {exc}")
        await ws2.close()


if __name__ == "__main__":
    unittest.main()
