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
from flixcli.pairgate import PairGate
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
        # These tests pin the operator-gated flow; auto-connect has its own
        # suite (tests/test_auto_connect.py).
        self.store.set_auto_connect(False)
        self.gate = PairGate(timeout=1.0)
        self.client = TestClient(TestServer(make_app(
            self.store, ME, lambda *_: None, pair_gate=self.gate)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self._tmp.cleanup()

    async def test_ask_without_correct_code_waits_for_operator(self):
        task = asyncio.create_task(self.client.post("/intent", json={
            "action": ACTION_ASK_PAIR, "deviceId": "attacker",
            "extra": {"verifyDevice": ME["fingerprint"], "verifyCode": "0000"}}))
        for _ in range(20):
            await asyncio.sleep(0.02)
            if self.gate.pending():
                break
        self.assertEqual([p["fingerprint"] for p in self.gate.pending()],
                         ["attacker"])
        self.assertTrue(self.gate.approve("attacker", False))
        r = await task
        self.assertEqual(r.status, 200)
        self.assertIn("301", await r.text())
        self.assertFalse(self.store.is_paired("attacker"))

    async def test_ask_without_code_pairs_after_operator_approval(self):
        task = asyncio.create_task(self.client.post("/intent", json={
            "action": ACTION_ASK_PAIR, "deviceId": "attacker",
            "extra": {"verifyDevice": ME["fingerprint"], "verifyCode": "0000"}}))
        for _ in range(20):
            await asyncio.sleep(0.02)
            if self.gate.pending():
                break
        self.assertTrue(self.gate.approve("attacker", True))
        r = await task
        self.assertIn("0: success", await r.text())
        self.assertTrue(self.store.is_paired("attacker"))

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

    def test_vmware_and_hyperv_adapters_are_not_lan_interfaces(self):
        import flixcli

        names = {15: "VMware Network Adapter VMnet1 VMware Virtual Ethernet",
                 24: "vEthernet (Default Switch) Hyper-V Virtual Ethernet",
                 4: "WLAN Intel(R) Wi-Fi 6E AX211",
                 18: "以太网 Realtek PCIe GbE Family Controller"}
        orig_names, orig_rows = flixcli._win_adapter_names, flixcli._win_ipv4_rows
        flixcli._win_adapter_names = lambda: names
        flixcli._win_ipv4_rows = lambda: [
            ("192.168.73.1", "255.255.255.0", 15),
            ("172.18.80.1", "255.255.240.0", 24),
            ("192.168.237.7", "255.255.254.0", 4),
            ("192.168.50.1", "255.255.255.0", 18),
        ]
        flixcli._ADAPTER_CACHE = (0.0, {})
        try:
            excluded = dict(flixcli.virtual_interfaces())
            self.assertIn("192.168.73.1", excluded)
            self.assertIn("172.18.80.1", excluded)
            self.assertNotIn("192.168.237.7", excluded)
            self.assertNotIn("192.168.50.1", excluded)
        finally:
            flixcli._win_adapter_names = orig_names
            flixcli._win_ipv4_rows = orig_rows
            flixcli._ADAPTER_CACHE = (0.0, {})

    def test_interface_choice_sticks_instead_of_chasing_the_route(self):
        import flixcli

        orig_names, orig_rows = flixcli._win_adapter_names, flixcli._win_ipv4_rows
        orig_routed, orig_gw, orig_last = (flixcli._routed_ip,
                                           flixcli._gateway_ifindexes,
                                           flixcli._last_iface)
        orig_override = flixcli._local_ip_override
        names = {4: "WLAN Intel(R) Wi-Fi 6E AX211",
                 15: "VMware Network Adapter VMnet1 VMware Virtual"}
        flixcli._win_adapter_names = lambda: names
        flixcli._win_ipv4_rows = lambda: [
            ("192.168.73.1", "255.255.255.0", 15),
            ("192.168.237.7", "255.255.254.0", 4),
        ]
        flixcli._gateway_ifindexes = lambda: {4}
        flixcli._local_ip_override = ""
        flixcli._ADAPTER_CACHE = (0.0, {})
        flixcli._last_iface = ""
        try:
            flixcli._routed_ip = lambda: "192.168.237.7"
            self.assertEqual(flixcli.default_iface_ip(), "192.168.237.7")
            # Wi-Fi blinks and the OS now routes through VMware: the announce
            # must not move onto a link no phone shares.
            flixcli._routed_ip = lambda: "192.168.73.1"
            self.assertEqual(flixcli.default_iface_ip(), "192.168.237.7")
        finally:
            flixcli._win_adapter_names = orig_names
            flixcli._win_ipv4_rows = orig_rows
            flixcli._routed_ip = orig_routed
            flixcli._gateway_ifindexes = orig_gw
            flixcli._last_iface = orig_last
            flixcli._local_ip_override = orig_override
            flixcli._ADAPTER_CACHE = (0.0, {})

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
