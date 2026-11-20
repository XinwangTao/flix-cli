"""Official-Flix bridge tests (2026-11-20).

Covers the three pieces that make "discover any version + talk to the
official client" work:
  * the sweep covers every physical subnet and our own address
  * an unknown ip:port is identified before the bubble is built
  * an explicit port picks the device row that owns it
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import flixcli
from flixcli import discovery, model
from flixcli.config import Store
from flixcli.outbox import send_text
from flixcli.routing import resolve_target


class LocalHostsTests(unittest.TestCase):
    def test_23_subnet_and_own_address_are_swept(self):
        with mock.patch.object(
                flixcli, "_win_ipv4_table",
                return_value=[("192.168.237.7", "255.255.254.0")]), \
                mock.patch.object(flixcli, "virtual_interfaces",
                                  return_value=[]):
            hosts = discovery.local_hosts()
        self.assertIn("192.168.236.182", hosts)   # other half of the /23
        self.assertIn("192.168.237.7", hosts)     # the local official client
        self.assertIn("127.0.0.1", hosts)

    def test_a_wide_nic_is_trimmed_to_one_24(self):
        with mock.patch.object(
                flixcli, "_win_ipv4_table",
                return_value=[("10.1.2.3", "255.255.0.0")]), \
                mock.patch.object(flixcli, "virtual_interfaces",
                                  return_value=[]):
            hosts = discovery.local_hosts()
        self.assertLessEqual(len(hosts), discovery.MAX_SWEEP_HOSTS)
        self.assertIn("10.1.2.3", hosts)


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite")
        self.store.upsert_device("old-fp", alias="THEWANG", port=18991,
                                 ip="192.168.1.9")
        self.store.upsert_device("official-fp", alias="TheWang", port=8891,
                                 ip="192.168.1.9")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_explicit_port_picks_that_device(self):
        ip, port, fp = resolve_target(self.store, "192.168.1.9:8891")
        self.assertEqual((ip, port, fp),
                         ("192.168.1.9", 8891, "official-fp"))

    def test_unknown_port_falls_back_to_the_raw_endpoint(self):
        ip, port, fp = resolve_target(self.store, "192.168.1.9:19091")
        self.assertEqual((ip, port, fp), ("192.168.1.9", 19091,
                                          "192.168.1.9:19091"))


class SendTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_endpoint_is_identified_before_sending(self):
        calls = {}

        async def fake_ping(ip, ident, port=8891, device_port=8891,
                            timeout=6.0, from_="manual"):
            calls["ping"] = (ip, port, device_port)
            return {"alias": "TheWang", "fingerprint": "official-fp",
                    "deviceModel": "Windows", "port": 8891, "version": 100,
                    "os": "windows"}

        async def fake_post(ip, bubble, port=8891, timeout=8.0):
            calls["bubble"] = (ip, port, dict(bubble))
            return "received bubble"

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            try:
                store.upsert_device("ip:192.168.1.9:8891", alias="192.168.1.9",
                                    port=8891, ip="192.168.1.9")
                ident = {"fingerprint": "me-fp", "alias": "me"}
                with mock.patch("flixcli.outbox.ping_v2", fake_ping), \
                        mock.patch("flixcli.outbox.post_bubble", fake_post):
                    result = await send_text(store, ident,
                                             "192.168.1.9:8891", "hi",
                                             device_port=18991)
                fingerprints = [row["fingerprint"] for row in store.devices()]
            finally:
                store.close()
        self.assertTrue(result.get("ok"))
        self.assertEqual(calls["ping"], ("192.168.1.9", 8891, 18991))
        self.assertEqual(calls["bubble"][2]["to"], "official-fp")
        self.assertIn("official-fp", fingerprints)
        self.assertNotIn("ip:192.168.1.9:8891", fingerprints)


if __name__ == "__main__":
    unittest.main()
