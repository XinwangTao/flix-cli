"""Auto-join / one-tap pairing tests (2026-11-17).

Covers the three pieces that make manual IP entry unnecessary:
  * active LAN scan (`scan_lan`) proves identity via /ping_v2
  * the pair gate holds a code-less ask until the operator decides
  * the desktop API exposes pending asks and can approve them
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from flixcli import desktop_api, model, pairgate
from flixcli.config import Store
from flixcli.discovery import scan_lan
from flixcli.pairgate import PairGate


class PairGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_approval_resolves_the_waiter(self):
        gate = PairGate(timeout=1.0)
        task = asyncio.create_task(gate.ask("fp-a", {"ip": "10.0.0.9"}))
        for _ in range(20):
            await asyncio.sleep(0.01)
            if gate.pending():
                break
        self.assertEqual(gate.pending()[0]["fingerprint"], "fp-a")
        self.assertTrue(gate.approve("fp-a", True))
        self.assertIs(await task, True)

    async def test_rejection_and_timeout_are_false(self):
        gate = PairGate(timeout=1.0)
        task = asyncio.create_task(gate.ask("fp-b", {}))
        await asyncio.sleep(0.01)
        self.assertTrue(gate.approve("fp-b", False))
        self.assertIs(await task, False)

        gate = PairGate(timeout=0.05)
        self.assertIs(await gate.ask("fp-c", {}), False)

    async def test_unknown_fingerprint_cannot_be_approved(self):
        gate = PairGate(timeout=0.05)
        self.assertFalse(gate.approve("nobody", True))


class ScanTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        peer = {"alias": "phone", "fingerprint": "peer-fp",
                "deviceModel": "Android", "deviceType": "tablet",
                "os": "android"}

        async def ping(request: web.Request) -> web.Response:
            payload = await request.json()
            modal = payload.get("deviceModal") or {}
            own = model.device_modal(peer, 8891, from_=modal.get("from", ""))
            return web.Response(
                text=json.dumps(json.dumps(own, separators=(",", ":")),
                                separators=(",", ":")),
                content_type="text/plain")

        app = web.Application()
        app.router.add_post("/ping_v2", ping)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def test_scan_finds_peer_and_filters_self(self):
        seen = []
        ident = {"fingerprint": "me-fp", "alias": "me"}
        found = await scan_lan(seen.append, ident, device_port=18991,
                               ports=(self.port,), hosts=["127.0.0.1"])
        self.assertEqual([d["fingerprint"] for d in found], ["peer-fp"])
        self.assertEqual(found[0]["ip"], "127.0.0.1")
        self.assertEqual([d["fingerprint"] for d in seen], ["peer-fp"])

    async def test_scan_skips_own_fingerprint(self):
        seen = []
        ident = {"fingerprint": "peer-fp", "alias": "me"}
        found = await scan_lan(seen.append, ident, ports=(self.port,),
                               hosts=["127.0.0.1"])
        self.assertEqual(found, [])
        self.assertEqual(seen, [])


class PendingPairApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "db.sqlite")
        self.ident = {"fingerprint": "me-fp", "alias": "me"}
        app, _publish = desktop_api.make_desktop_app(
            self.store, self.ident, lambda *args: None)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self._tmp.cleanup()

    async def test_overview_lists_pending_and_approve_resolves(self):
        gate = pairgate.GATE
        task = asyncio.create_task(gate.ask("pending-fp", {"ip": "10.0.0.3"}))
        for _ in range(20):
            await asyncio.sleep(0.01)
            if gate.pending():
                break
        resp = await self.client.get("/api/overview")
        body = await resp.json()
        self.assertIn("pendingPairs", body)
        self.assertEqual(body["pendingPairs"][0]["fingerprint"], "pending-fp")
        resp = await self.client.post(
            "/api/pair/approve", json={"fingerprint": "pending-fp", "accept": True})
        self.assertEqual(resp.status, 200)
        self.assertIs(await task, True)

    async def test_approve_unknown_is_404(self):
        resp = await self.client.post(
            "/api/pair/approve", json={"fingerprint": "nobody", "accept": True})
        self.assertEqual(resp.status, 404)


if __name__ == "__main__":
    unittest.main()


class FirstContactTests(unittest.IsolatedAsyncioTestCase):
    """A discovered, unpaired peer (the official app) gets one prompt."""

    async def asyncSetUp(self):
        from flixcli.server import make_app
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "db.sqlite")
        self.ident = {"fingerprint": "me-fp", "alias": "me"}
        self.store.upsert_device("official-fp", alias="OPPO", port=8891,
                                 version=132, ip="127.0.0.1", os_name="android")
        self.store.con.commit()
        self.gate = PairGate(timeout=1.0)
        self.app = make_app(self.store, self.ident, lambda *_: None,
                            pair_gate=self.gate)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self._tmp.cleanup()

    def _bubble(self):
        return {"id": "b-1", "from": "official-fp", "to": "me-fp", "type": 0,
                "time": 1, "groupId": "", "content": "hello"}

    async def test_first_message_prompts_then_pairs(self):
        async def fake_ping(*args, **kwargs):
            return {"fingerprint": "official-fp"}

        with mock.patch("flixcli.channel.ping_v2", fake_ping):
            resp = await self.client.post("/bubble", json=self._bubble())
            self.assertEqual(resp.status, 403)
            for _ in range(50):
                await asyncio.sleep(0.01)
                if self.gate.pending():
                    break
            self.assertEqual([p["fingerprint"] for p in self.gate.pending()],
                             ["official-fp"])
            self.assertTrue(self.gate.approve("official-fp", True))
            for _ in range(50):
                await asyncio.sleep(0.01)
                if self.store.is_paired("official-fp"):
                    break
            self.assertTrue(self.store.is_paired("official-fp"))
        resp = await self.client.post("/bubble", json=self._bubble())
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.text(), "received bubble")

    async def test_forged_sender_never_prompts(self):
        bubble = self._bubble()
        bubble["from"] = "not-discovered"
        resp = await self.client.post("/bubble", json=bubble)
        self.assertEqual(resp.status, 403)
        await asyncio.sleep(0.05)
        self.assertEqual(self.gate.pending(), [])


class LatePairApprovalTests(unittest.IsolatedAsyncioTestCase):
    """The ask must answer before the peer's 8 s timeout and confirm later."""

    async def asyncSetUp(self):
        from flixcli.server import make_app
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "db.sqlite")
        self.ident = {"fingerprint": "me-fp", "alias": "me"}
        self.store.upsert_device("late-fp", alias="phone", port=8899,
                                 version=1, ip="127.0.0.1", os_name="android")
        self.store.con.commit()
        self.gate = PairGate(timeout=2.0, fast_timeout=0.05)
        self.app = make_app(self.store, self.ident, lambda *_: None,
                            pair_gate=self.gate)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self._tmp.cleanup()

    async def test_late_approval_confirms_peer(self):
        codes = []
        async def fake_post(ip, intent, port=8891, timeout=8.0):
            codes.append((ip, port, intent))
            return "{0: success}"

        bad = "0000" if self.store.own_pair_key() != "0000" else "0001"
        body = {"action": "askPairDevice", "deviceId": "late-fp",
                "extra": {"verifyDevice": "me-fp", "verifyCode": bad}}
        with mock.patch("flixcli.channel.post_intent", fake_post):
            resp = await self.client.post("/intent", json=body)
            self.assertIn("301", await resp.text())
            self.assertTrue(self.gate.is_pending("late-fp"))
            self.assertTrue(self.gate.approve("late-fp", True))
            for _ in range(100):
                await asyncio.sleep(0.01)
                if self.store.is_paired("late-fp") and codes:
                    break
        self.assertTrue(self.store.is_paired("late-fp"))
        self.assertEqual(codes[0][0], "127.0.0.1")
        self.assertEqual(codes[0][1], 8899)
        self.assertEqual(codes[0][2].get("action"), "confirmPairDevice")

    async def test_late_approval_resolves_current_port(self):
        """A peer that moved its channel port is reached via its ping modal."""
        self.store.upsert_device("late2-fp", alias="phone2", port=8899,
                                 version=1, ip="127.0.0.1", os_name="android")
        self.store.con.commit()
        codes = []

        async def fake_ping(ip, ident, port=8891, device_port=8891, timeout=6.0,
                            from_="manual"):
            return {"fingerprint": "late2-fp", "port": 19001}

        async def fake_post(ip, intent, port=8891, timeout=8.0):
            codes.append((ip, port, intent))
            return "{0: success}"

        bad = "0000" if self.store.own_pair_key() != "0000" else "0001"
        body = {"action": "askPairDevice", "deviceId": "late2-fp",
                "extra": {"verifyDevice": "me-fp", "verifyCode": bad}}
        with mock.patch("flixcli.channel.ping_v2", fake_ping), \
                mock.patch("flixcli.channel.post_intent", fake_post):
            resp = await self.client.post("/intent", json=body)
            self.assertIn("301", await resp.text())
            self.assertTrue(self.gate.approve("late2-fp", True))
            for _ in range(100):
                await asyncio.sleep(0.01)
                if codes:
                    break
        self.assertEqual(codes[0][0], "127.0.0.1")
        self.assertEqual(codes[0][1], 19001)
