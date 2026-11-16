"""Scan-to-connect: one QR for pairing plus model-gateway config, and the
wireless-ADB discovery parsers."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit

from aiohttp.test_utils import TestClient, TestServer

from flixcli import desktop_api
from flixcli.adb_wireless import (parse_adb_mdns, parse_browse, parse_ipv4,
                                  parse_resolve)
from flixcli.config import Store
from flixcli.model_gateway import load_config, save_config, share_info
from flixcli.qrcode import pair_payload, pair_uri, parse_payload


class PairUriTests(unittest.TestCase):
    def test_link_carries_pairing_fields(self):
        uri = pair_uri("fp123", 8891, "0420", ips=["192.168.1.7"])
        parts = urlsplit(uri)
        self.assertEqual(parts.scheme, "relaydesk")
        self.assertEqual(parts.netloc, "pair")
        query = parse_qs(parts.query)
        self.assertEqual(query["fp"], ["fp123"])
        self.assertEqual(query["ip"], ["192.168.1.7"])
        self.assertEqual(query["port"], ["8891"])
        self.assertEqual(query["code"], ["0420"])
        self.assertNotIn("model_base", query)

    def test_model_fields_need_a_base(self):
        without = pair_uri("fp", 8891, "1", ips=["10.0.0.2"],
                           model_token="rd-secret")
        self.assertNotIn("model_token", parse_qs(urlsplit(without).query))
        with_model = pair_uri("fp", 8891, "1", ips=["10.0.0.2"],
                              model_base="http://10.0.0.2:18993",
                              model_token="rd-secret")
        query = parse_qs(urlsplit(with_model).query)
        self.assertEqual(query["model_base"], ["http://10.0.0.2:18993"])
        self.assertEqual(query["model_token"], ["rd-secret"])

    def test_flix2_payload_is_unchanged(self):
        payload = pair_payload("fp", 8891, "7", ips=["10.0.0.2"])
        self.assertTrue(payload.startswith("flix2://"))
        self.assertEqual(parse_payload(payload)["ips"], ["10.0.0.2"])


class ShareInfoTests(unittest.TestCase):
    def test_disabled_gateway_shares_nothing(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            try:
                self.assertIsNone(share_info(store))
            finally:
                store.close()

    def test_enabled_gateway_returns_address_and_token(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            try:
                save_config(store, enabled=True, port=18993)
                info = share_info(store)
                token = load_config(store)["token"]
                self.assertIsNotNone(info)
                self.assertTrue(info["base"].startswith("http://"))
                self.assertTrue(info["base"].endswith(":18993"))
                self.assertEqual(info["token"], token)
            finally:
                store.close()


class WirelessParserTests(unittest.TestCase):
    def test_browse_takes_the_last_added_instance(self):
        output = ("14:52:01.123  Add        2  12 local.  "
                  "_adb-tls-connect._tcp.  adb-OLD\n"
                  "14:52:02.456  Add        2  12 local.  "
                  "_adb-tls-connect._tcp.  adb-NEW")
        self.assertEqual(parse_browse(output), "adb-NEW")

    def test_browse_without_add_is_empty(self):
        self.assertEqual(parse_browse("no services"), "")

    def test_resolve_reads_host_and_port(self):
        output = ("14:52:03.456  adb-XXXX._adb-tls-connect._tcp. can be "
                  "reached at phone.local.:41234 (interface 12)")
        self.assertEqual(parse_resolve(output), ("phone.local.", 41234))

    def test_resolve_without_reached_at(self):
        self.assertIsNone(parse_resolve("nothing here"))

    def test_ipv4_skips_link_local(self):
        output = ("14:52:05.789  phone.local.  Add  2  12  "
                  "169.254.3.4  192.168.1.23")
        self.assertEqual(parse_ipv4(output), "192.168.1.23")
        self.assertEqual(parse_ipv4("no address"), "")

    def test_adb_mdns_services_line(self):
        output = ("List of discovered mdns services\n"
                  "adb-XXXX\t_adb-tls-connect._tcp\t192.168.1.23:41234")
        self.assertEqual(parse_adb_mdns(output),
                         ("192.168.1.23", 41234, "adb-XXXX"))
        self.assertIsNone(parse_adb_mdns("List of discovered mdns services"))


class PairLinkApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "db.sqlite")
        app, _ = desktop_api.make_desktop_app(
            self.store, {"fingerprint": "fp-me", "alias": "me"}, lambda *a: None)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self._tmp.cleanup()

    async def test_link_without_sharing_has_no_model_fields(self):
        body = await (await self.client.get("/api/pair-link")).json()
        self.assertTrue(body["ok"])
        self.assertIsNone(body["model"])
        query = parse_qs(urlsplit(body["link"]).query)
        self.assertEqual(query["fp"], ["fp-me"])
        self.assertNotIn("model_base", query)

    async def test_link_carries_model_when_sharing_is_on(self):
        save_config(self.store, enabled=True, port=18993)
        body = await (await self.client.get("/api/pair-link")).json()
        query = parse_qs(urlsplit(body["link"]).query)
        self.assertEqual(query["model_base"], [body["model"]["base"]])
        self.assertEqual(query["model_token"], [body["model"]["token"]])


class RelaySourceTests(unittest.TestCase):
    def test_embedded_relay_source_compiles(self):
        from flixcli.adb_wireless import RELAY_SOURCE
        compile(RELAY_SOURCE, "flix_adb_relay_script.py", "exec")

    def test_relay_script_is_materialised_for_apple_python(self):
        from flixcli.adb_wireless import _relay_script_path
        path = _relay_script_path()
        self.assertTrue(path.exists())
        self.assertIn("relay listening", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
