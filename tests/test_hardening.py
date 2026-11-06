"""Regression tests for the 2026-11 audit hardening.

Each test replays an attack that succeeded on v2.2.1 or pins a fix whose
absence was reproduced on a live daemon.
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from flixcli import __version__, default_iface_ip, is_virtual_ip
from flixcli.config import Store
from flixcli.model import (ACTION_ASK_PAIR, ACTION_CLIPBOARD, ACTION_SHELL,
                           BUBBLE_TEXT)
from flixcli.server import make_app
from flixcli.webapp import make_web_app

ME = {"fingerprint": "self-fp", "alias": "me", "deviceModel": "Windows"}
PEER = "peer-fp"


def _store(root: Path) -> Store:
    store = Store(root / "db.sqlite")
    store.set_pref("saveDir", str(root / "downloads"))
    return store


class PairingHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = _store(self.root)
        self.client = TestClient(TestServer(make_app(
            self.store, ME, lambda *_: None)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self._tmp.cleanup()

    async def test_ask_without_correct_code_is_rejected(self):
        r = await self.client.post("/intent", json={
            "action": ACTION_ASK_PAIR, "deviceId": "attacker",
            "extra": {"verifyDevice": ME["fingerprint"], "verifyCode": "0000"}})
        self.assertEqual(r.status, 200)
        self.assertIn("302", await r.text())
        self.assertFalse(self.store.is_paired("attacker"))

    async def test_ask_with_own_code_pairs_and_records_source(self):
        r = await self.client.post("/intent", json={
            "action": ACTION_ASK_PAIR, "deviceId": "attacker",
            "extra": {"verifyDevice": ME["fingerprint"],
                      "verifyCode": self.store.own_pair_key()}})
        self.assertIn("0: success", await r.text())
        self.assertTrue(self.store.is_paired("attacker"))
        row = self.store.con.execute(
            "SELECT ip FROM persistence_devices WHERE fingerprint='attacker'"
        ).fetchone()
        self.assertEqual(row[0], "127.0.0.1")

    async def test_confirm_from_unknown_source_is_rejected(self):
        r = await self.client.post("/intent", json={
            "action": "confirmPairDevice", "deviceId": "stranger",
            "extra": {"verifyDevice": "stranger", "verifyCode": "1234",
                      "accept": "true"}})
        self.assertIn("302", await r.text())
        self.assertFalse(self.store.is_paired("stranger"))

    async def test_confirm_from_known_source_is_accepted(self):
        self.store.upsert_device("stranger", ip="127.0.0.1")
        r = await self.client.post("/intent", json={
            "action": "confirmPairDevice", "deviceId": "stranger",
            "extra": {"verifyDevice": "stranger", "verifyCode": "1234",
                      "accept": "true"}})
        self.assertIn("0: success", await r.text())
        self.assertTrue(self.store.is_paired("stranger"))


class MessageHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = _store(self.root)
        self.client = TestClient(TestServer(make_app(
            self.store, ME, lambda *_: None)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self._tmp.cleanup()

    def _bubble(self, sender: str) -> dict:
        return {"id": str(uuid.uuid4()), "from": sender, "to": ME["fingerprint"],
                "type": BUBBLE_TEXT, "time": 1, "groupId": "",
                "content": "hello"}

    async def test_unpaired_bubble_is_rejected(self):
        r = await self.client.post("/bubble", json=self._bubble("stranger"))
        self.assertEqual(r.status, 403)
        self.assertEqual(self.store.bubbles(), [])

    async def test_paired_but_wrong_source_is_rejected(self):
        self.store.add_pair(PEER, "1234")
        self.store.upsert_device(PEER, ip="10.9.9.9")
        r = await self.client.post("/bubble", json=self._bubble(PEER))
        self.assertEqual(r.status, 403)
        self.assertEqual(self.store.bubbles(), [])

    async def test_paired_from_known_source_is_accepted(self):
        self.store.add_pair(PEER, "1234")
        self.store.upsert_device(PEER, ip="127.0.0.1")
        r = await self.client.post("/bubble", json=self._bubble(PEER))
        self.assertEqual(r.status, 200)
        self.assertEqual(len(self.store.bubbles()), 1)

    async def test_clipboard_from_unpaired_is_rejected(self):
        clipped: list[str] = []

        async def setter(text: str) -> None:
            clipped.append(text)

        self.client.server.app  # keep reference; rebuild with a setter
        app = make_app(self.store, ME, lambda *_: None,
                       clipboard_setter=setter)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.post("/intent", json={
                "action": ACTION_CLIPBOARD, "deviceId": "stranger",
                "extra": {"text": json.dumps({"text": "evil"})}})
            self.assertEqual(r.status, 403)
            self.assertEqual(clipped, [])
        finally:
            await client.close()

    async def test_rd_shell_requires_paired_and_matching_source(self):
        self.store.add_pair(PEER, "1234")
        self.store.upsert_device(PEER, ip="10.9.9.9")
        payload = {"action": ACTION_SHELL, "deviceId": PEER,
                   "extra": {"id": "x1", "command": "echo HARDENED"}}
        r = await self.client.post("/intent", json=payload)
        self.assertEqual(r.status, 403)

        self.store.con.execute(
            "UPDATE persistence_devices SET ip='127.0.0.1' WHERE fingerprint=?",
            (PEER,))
        self.store.con.commit()
        r = await self.client.post("/intent", json=payload)
        self.assertEqual(r.status, 200)
        self.assertIn("HARDENED", await r.text())


class WebHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = _store(self.root)
        app, _push = make_web_app(
            self.store, ME, lambda *_: None,
            lambda: str(self.root / "downloads"), 8891)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self._tmp.cleanup()

    async def test_upload_filename_cannot_escape_save_dir(self):
        # Raw multipart body: aiohttp's client percent-encodes slashes, so the
        # real daemon (and curl-style clients) is what reaches the handler.
        boundary = "----flixtest"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; '
            'filename="../../evil.txt"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + b"evil" + f"\r\n--{boundary}--\r\n".encode()
        r = await self.client.post(
            f"/upload?pin={self.store.own_web_pin()}", data=body,
            headers={"Content-Type":
                     f"multipart/form-data; boundary={boundary}"})
        # Either reject it or flatten to the basename — never escape.
        self.assertIn(r.status, (200, 400))
        self.assertFalse((self.root / "evil.txt").exists())
        self.assertFalse((self.root.parent / "evil.txt").exists())
        if r.status == 200:
            self.assertTrue(
                (self.root / "downloads" / "evil.txt").exists(),
                "sanitized file must stay inside saveDir")

    async def test_upload_normal_name_still_works(self):
        form = FormData()
        form.add_field("file", b"ok", filename="ok.txt",
                       content_type="application/octet-stream")
        r = await self.client.post(
            f"/upload?pin={self.store.own_web_pin()}", data=form)
        self.assertEqual(r.status, 200)
        self.assertTrue((self.root / "downloads" / "ok.txt").exists())

    async def test_pin_probing_is_throttled_on_get_route(self):
        for _ in range(10):
            r = await self.client.get("/0000", allow_redirects=False)
            self.assertEqual(r.status, 302)
        pin = self.store.own_web_pin()
        r = await self.client.get(f"/{pin}", allow_redirects=False)
        # Throttled even with the right PIN: the counter is per source.
        self.assertEqual(r.status, 302)


class InterfaceAndVersionTests(unittest.TestCase):
    def test_version_comes_from_package_metadata(self):
        self.assertEqual(__version__, importlib.metadata.version("flixcli"))

    def test_virtual_ranges_are_never_lan_interfaces(self):
        for ip in ("198.18.0.1", "100.64.0.1", "169.254.1.1", "127.0.0.1"):
            self.assertTrue(is_virtual_ip(ip), ip)

    def test_tun_is_skipped_even_with_a_slash_30_mask(self):
        import flixcli

        def fake_table():
            return [("198.18.0.1", "255.255.255.252"),
                    ("10.37.73.198", "255.255.255.0")]

        orig_table, orig_routed = flixcli._win_ipv4_table, flixcli._routed_ip
        orig_gw = flixcli._gateway_ifindexes
        flixcli._win_ipv4_table = fake_table
        flixcli._routed_ip = lambda: "198.18.0.1"
        flixcli._gateway_ifindexes = lambda: set()
        try:
            self.assertEqual(default_iface_ip(), "10.37.73.198")
        finally:
            (flixcli._win_ipv4_table, flixcli._routed_ip,
             flixcli._gateway_ifindexes) = orig_table, orig_routed, orig_gw


if __name__ == "__main__":
    unittest.main()
