"""Regression tests for the operational hardening batch (2026-11 audit)."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from flixcli.cli import _join_remote_command
from flixcli.config import Store
from flixcli.desktop_api import make_desktop_app
from flixcli.media_session import SessionError, SessionManager
from flixcli.remoteshell import MAX_OUTPUT_BYTES, run_command
from flixcli.server import make_app

ME = {"fingerprint": "me-fp", "alias": "me", "deviceModel": "Windows"}


class ShellQuotingTests(unittest.TestCase):
    def test_arguments_with_spaces_survive(self):
        parts = ["python", "-c", "print('a b')"]
        joined = _join_remote_command(parts)
        if sys.platform == "win32":
            self.assertEqual(joined, 'python -c "print(\'a b\')"')
        else:
            self.assertIn("'print('\"'\"'a b'\"'\"')'", joined)

    def test_empty(self):
        self.assertEqual(_join_remote_command([]), "")


class RemoteShellBoundsTests(unittest.TestCase):
    def test_huge_output_is_truncated_without_buffering_everything(self):
        code = "import sys; sys.stdout.write('x'*1200000)"
        result = run_command(f'"{sys.executable}" -c "{code}"', timeout=30)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["stdout"]), MAX_OUTPUT_BYTES + 8)
        self.assertEqual(result["exit"], 0)

    def test_timeout_kills_the_process_tree(self):
        cmd = f'"{sys.executable}" -c "import time; time.sleep(30)"'
        started = time.monotonic()
        result = run_command(cmd, timeout=1)
        self.assertTrue(result["timeout"])
        self.assertLess(time.monotonic() - started, 15)


class OutboxRetryTests(unittest.TestCase):
    def test_retry_resets_created_so_expiry_does_not_eat_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            try:
                old = int(time.time() * 1000) - store.OUTBOX_TTL_MS - 1000
                store.con.execute(
                    "INSERT INTO outbox (id,kind,peer,payload,state,created)"
                    " VALUES ('o1','text','peer','{}','expired',?)", (old,))
                store.con.commit()
                self.assertTrue(store.outbox_retry("o1"))
                row = store.con.execute(
                    "SELECT state, created FROM outbox WHERE id='o1'").fetchone()
                self.assertEqual(row[0], "queued")
                self.assertGreater(row[1], old)
            finally:
                store.close()


class DesktopApiGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = Store(self.root / "db.sqlite")
        app, _ = make_desktop_app(self.store, ME, lambda *_: {"ok": True},
                                  api_port=8898)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self._tmp.cleanup()

    async def test_foreign_host_is_rejected(self):
        r = await self.client.get("/api/self", headers={"Host": "evil.example"})
        self.assertEqual(r.status, 403)

    async def test_foreign_origin_is_rejected(self):
        r = await self.client.post("/api/send-text",
                                   json={"to": "x", "text": "y"},
                                   headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status, 403)

    async def test_plain_text_post_cannot_be_a_simple_csrf_request(self):
        r = await self.client.post(
            "/api/send-text", data='{"to":"x","text":"y"}',
            headers={"Content-Type": "text/plain"})
        self.assertEqual(r.status, 415)

    async def test_normal_local_json_request_passes(self):
        r = await self.client.get("/api/self")
        self.assertEqual(r.status, 200)


class WireLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_is_opt_in_and_redacts_bodies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / "wire.log"
            store = Store(root / "db.sqlite")
            store.upsert_device("peer", ip="127.0.0.1")
            store.add_pair("peer", "1234")
            app = make_app(store, ME, lambda *_: None)
            client = TestClient(TestServer(app))
            await client.start_server()
            old = os.environ.get("FLIX_WIRE_LOG")
            try:
                os.environ.pop("FLIX_WIRE_LOG", None)
                await client.post("/ping_v2", json={
                    "deviceModal": {"from": "peer", "secret": "TOP-SECRET"}})
                self.assertFalse(log.exists())

                os.environ["FLIX_WIRE_LOG"] = str(log)
                await client.post("/ping_v2", json={
                    "deviceModal": {"from": "peer", "secret": "TOP-SECRET"}})
                text = log.read_text(encoding="utf-8", errors="replace")
                self.assertNotIn("TOP-SECRET", text)
                self.assertIn("sha256=", text)
            finally:
                await client.close()
                store.close()
                if old is None:
                    os.environ.pop("FLIX_WIRE_LOG", None)
                else:
                    os.environ["FLIX_WIRE_LOG"] = old


class MediaCallIdTests(unittest.TestCase):
    def test_non_uuid_call_id_is_rejected(self):
        mgr = SessionManager("me-fp", lambda fp: True)
        with self.assertRaises(SessionError) as ctx:
            mgr.request("peer-fp", ["view"],
                        call_id="../../../../Users/jlc/Desktop/x")
        self.assertEqual(ctx.exception.kind, "bad-call-id")

    def test_uuid_call_id_is_accepted(self):
        mgr = SessionManager("me-fp", lambda fp: True)
        session = mgr.request("peer-fp", ["view"],
                              call_id="12345678-1234-5678-1234-567812345678")
        self.assertEqual(session["callId"],
                         "12345678-1234-5678-1234-567812345678")


if __name__ == "__main__":
    unittest.main()
