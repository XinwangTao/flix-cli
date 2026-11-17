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

    def test_dead_gateway_is_reported_without_waiting_for_a_probe(self):
        import socket
        import time
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            try:
                # Bind then close: the port is certainly nobody's now.
                sock = socket.socket()
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
                sock.close()
                save_config(store, enabled=True, port=port)
                started = time.monotonic()
                info = share_info(store)
                elapsed = time.monotonic() - started
                self.assertFalse(info["listening"])
                self.assertLess(elapsed, 0.5,
                                "端口没人监听时不该等满探测超时")
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

    async def test_pair_link_probe_does_not_block_the_event_loop(self):
        import asyncio
        import time
        from unittest import mock
        from flixcli import model_gateway

        def slow_share(_store):
            time.sleep(0.6)  # same shape as a gateway probe that has to wait
            return None

        with mock.patch.object(model_gateway, "share_info", side_effect=slow_share):
            pending = asyncio.create_task(self.client.get("/api/pair-link"))
            await asyncio.sleep(0.1)
            started = time.monotonic()
            other = await self.client.get("/api/self")
            elapsed = time.monotonic() - started
            self.assertEqual(other.status, 200)
            await pending
        self.assertLess(elapsed, 0.3,
                        "同步探测阻塞了事件循环，其他接口被卡住")

    async def test_link_carries_model_when_sharing_is_on(self):
        save_config(self.store, enabled=True, port=18993)
        body = await (await self.client.get("/api/pair-link")).json()
        query = parse_qs(urlsplit(body["link"]).query)
        self.assertEqual(query["model_base"], [body["model"]["base"]])
        self.assertEqual(query["model_token"], [body["model"]["token"]])


class WirelessConnectTests(unittest.TestCase):
    def test_connect_stops_the_relay_when_adb_never_sees_the_device(self):
        from unittest import mock
        from flixcli import adb_wireless

        stopped = []

        class FakeRun:
            def __init__(self, stdout=""):
                self.stdout = stdout
                self.stderr = ""
                self.returncode = 0

        def fake_run(argv, **kwargs):
            if argv[1] == "connect":
                return FakeRun("connected to 127.0.0.1:40009\n")
            return FakeRun("List of devices attached\n")  # device never appears

        with mock.patch.object(adb_wireless, "discover",
                               return_value={"ip": "10.0.0.9", "port": 41000,
                                             "instance": "adb-test"}), \
             mock.patch.object(adb_wireless, "start_relay",
                               return_value={"listen": 40009, "pid": 1,
                                             "log": ""}), \
             mock.patch.object(adb_wireless, "stop_relay",
                               side_effect=lambda port: stopped.append(port)), \
             mock.patch.object(adb_wireless.shutil, "which",
                               return_value="/usr/bin/adb"), \
             mock.patch.object(adb_wireless.subprocess, "run", fake_run):
            with self.assertRaises(adb_wireless.AdbWirelessError) as ctx:
                adb_wireless.connect()
        self.assertEqual(ctx.exception.kind, "connect-failed")
        self.assertEqual(stopped, [40009], "失败的连接不该留下中继进程")

    def test_frozen_bundle_refuses_to_be_used_as_an_interpreter(self):
        from unittest import mock
        from flixcli import adb_wireless
        with mock.patch.object(adb_wireless.os.path, "exists", return_value=False), \
             mock.patch.object(adb_wireless.sys, "frozen", True, create=True):
            with self.assertRaises(adb_wireless.AdbWirelessError) as ctx:
                adb_wireless._system_python()
        self.assertEqual(ctx.exception.kind, "relay-python")


class PrefsPermissionTests(unittest.TestCase):
    def test_prefs_file_holding_the_token_is_owner_only(self):
        import os
        import stat
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            try:
                store.set_pref("modelGateway", {"token": "rd-secret"})
                mode = stat.S_IMODE(os.stat(store.prefs_path).st_mode)
                self.assertEqual(mode & 0o077, 0,
                                 f"其他本地用户可读令牌: {oct(mode)}")
            finally:
                store.close()


class PairingPayloadTests(unittest.TestCase):
    def test_payload_matches_the_format_the_phone_expects(self):
        from flixcli.adb_wireless import pairing_payload
        payload, service, password = pairing_payload()
        self.assertEqual(payload, f"WIFI:T:ADB;S:{service};P:{password};;")
        self.assertTrue(service.startswith("flix-"))
        self.assertEqual(len(password), 6)
        self.assertNotIn("H:", payload, "官方配对码不带 host/port")

    def test_each_call_gets_fresh_credentials(self):
        from flixcli.adb_wireless import pairing_payload
        first = pairing_payload()
        second = pairing_payload()
        self.assertNotEqual(first[1], second[1])
        self.assertNotEqual(first[2], second[2])


class AdbPairApiTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_missing_credentials_is_a_bad_request(self):
        resp = await self.client.post("/api/adb/pair", json={})
        body = await resp.json()
        self.assertEqual(resp.status, 400)
        self.assertEqual(body["error"], "bad-params")


class AdbConnectApiTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_missing_device_reports_a_readable_error(self):
        from unittest import mock
        from flixcli import adb_wireless
        with mock.patch.object(adb_wireless, "connect",
                               side_effect=adb_wireless.AdbWirelessError(
                                   "no-service", "未发现无线调试服务")):
            resp = await self.client.post("/api/adb/connect", json={})
        body = await resp.json()
        self.assertEqual(resp.status, 503)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "no-service")


class RelayLifecycleTests(unittest.TestCase):
    def test_stop_relay_never_kills_a_stranger_and_keeps_the_pid_file(self):
        import subprocess
        from flixcli import adb_wireless

        proc = subprocess.Popen(["sleep", "5"])
        pid_file = adb_wireless._pid_file(49999)
        try:
            pid_file.write_text(str(proc.pid), encoding="utf-8")
            self.assertFalse(adb_wireless.stop_relay(49999))
            self.assertIsNone(proc.poll(), "不是本工具启动的进程，不能被它杀掉")
            self.assertTrue(pid_file.exists(), "没杀成功就要保留 pidfile，否则进程失联")
        finally:
            proc.kill()
            proc.wait()
            pid_file.unlink(missing_ok=True)


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
