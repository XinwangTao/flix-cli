"""Auto-connect: discovery -> pairing with no operator in the loop.

Two guarantees are covered:
  * the trust switch (prefs autoConnect) can turn the feature off, globally or
    per device;
  * the receiving side pairs only after /ping_v2 proves the fingerprint really
    answers at the request's source address - a claimed fingerprint is not
    identity, it is public on the wire.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from flixcli import model
from flixcli.config import Store
from flixcli.pairgate import PairGate
from flixcli.server import make_app


def _ask(auto=True, code="0000"):
    return model.pair_intent("peer-fp", "me-fp", code, auto=auto)


class PrefTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "db.sqlite")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_default_is_on(self):
        self.assertTrue(self.store.auto_connect_enabled())
        self.assertTrue(self.store.auto_connect_enabled("any-fp"))

    def test_global_off(self):
        self.store.set_auto_connect(False)
        self.assertFalse(self.store.auto_connect_enabled())
        self.assertFalse(self.store.auto_connect_enabled("any-fp"))

    def test_per_device_override(self):
        self.store.set_auto_connect(False)
        self.store.set_auto_connect(True, "trusted-fp")
        self.assertTrue(self.store.auto_connect_enabled("trusted-fp"))
        self.assertFalse(self.store.auto_connect_enabled("other-fp"))
        self.store.set_auto_connect(False, "trusted-fp")
        self.assertFalse(self.store.auto_connect_enabled("trusted-fp"))


class AutoPairServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "db.sqlite")
        self.ident = {"fingerprint": "me-fp", "alias": "me"}
        self.store.upsert_device("peer-fp", alias="PEER", port=18991,
                                 version=1, ip="127.0.0.1", os_name="windows")
        self.store.con.commit()
        self.gate = PairGate(timeout=1.0)
        self.app = make_app(self.store, self.ident, lambda *_: None,
                            pair_gate=self.gate, device_port=18991)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self._tmp.cleanup()

    async def test_wrong_code_pairs_when_identity_answers(self):
        async def fake_ping(*args, **kwargs):
            return {"fingerprint": "peer-fp"}

        with mock.patch("flixcli.channel.ping_v2", fake_ping):
            resp = await self.client.post("/intent", json=_ask())
            # The ask itself never waits on the proof (the peer's HTTP budget
            # is 8s); the decision arrives as a confirm.
            self.assertEqual(await resp.text(), "{301: pair Device Null}")
            await self._wait_paired("peer-fp")
        self.assertTrue(self.store.is_paired("peer-fp"))
        self.assertEqual(self.gate.pending(), [])

    async def test_forged_fingerprint_is_not_paired(self):
        async def fake_ping(*args, **kwargs):
            return {"fingerprint": "someone-else"}

        with mock.patch("flixcli.channel.ping_v2", fake_ping):
            resp = await self.client.post("/intent", json=_ask())
            self.assertEqual(await resp.text(), "{301: pair Device Null}")
            for _ in range(30):
                await self._tick()
        self.assertFalse(self.store.is_paired("peer-fp"))

    async def test_official_ask_without_the_auto_flag_still_prompts(self):
        """Official clients never send extra.auto: their one-tap flow keeps
        the operator prompt even while our auto-connect switch is on."""
        async def fake_ping(*args, **kwargs):
            return {"fingerprint": "peer-fp"}

        with mock.patch("flixcli.channel.ping_v2", fake_ping):
            resp = await self.client.post("/intent", json=_ask(auto=False))
        self.assertEqual(await resp.text(), "{301: pair Device Null}")
        for _ in range(50):
            await self._tick()
            if self.gate.pending():
                break
        self.assertEqual([p["fingerprint"] for p in self.gate.pending()],
                         ["peer-fp"])
        self.assertFalse(self.store.is_paired("peer-fp"))

    async def test_switch_off_keeps_the_gate(self):
        self.store.set_auto_connect(False)
        async def fake_ping(*args, **kwargs):
            return {"fingerprint": "peer-fp"}

        with mock.patch("flixcli.channel.ping_v2", fake_ping):
            resp = await self.client.post("/intent", json=_ask())
            self.assertEqual(await resp.text(), "{301: pair Device Null}")
            for _ in range(30):
                await self._tick()
        self.assertFalse(self.store.is_paired("peer-fp"))
        for _ in range(50):
            await self._tick()
            if self.gate.pending():
                break
        self.assertEqual([p["fingerprint"] for p in self.gate.pending()],
                         ["peer-fp"])

    async def test_correct_code_pairs_without_ping(self):
        self.store.set_auto_connect(False)
        key = self.store.own_pair_key()
        resp = await self.client.post("/intent", json=_ask(code=key))
        self.assertEqual(await resp.text(), "{0: success}")
        self.assertTrue(self.store.is_paired("peer-fp"))

    async def _wait_paired(self, fp, tries=100):
        for _ in range(tries):
            if self.store.is_paired(fp):
                return
            await self._tick()
        self.fail(f"{fp} was not paired")

    async def _tick(self):
        import asyncio
        await asyncio.sleep(0.01)


if __name__ == "__main__":
    unittest.main()
