"""POST /api/pair — desktop-initiated pairing over the real aiohttp app."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from flixcli import channel, desktop_api
from flixcli.config import Store

PEER = "fp-peer-tablet"


class PairApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "relaydesk.db")
        self.store.upsert_device(PEER, alias="TABLET", device_model="Android",
                                 port=8891, version=1, ip="10.0.0.2", os_name="android")
        self.store.con.commit()
        self.ident = {"fingerprint": "fp-me", "alias": "me"}
        app, self.publish = desktop_api.make_desktop_app(
            self.store, self.ident, lambda *args: None)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.calls = []

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self._tmp.cleanup()

    async def test_success_adds_pair_row(self):
        async def fake_pair(ip, my_fp, peer_fp, code, port=8891, timeout=6.0,
                            action="askPairDevice"):
            self.calls.append((ip, my_fp, peer_fp, port))
            return 0, "{0: success}"

        with mock.patch.object(channel, "pair_device", fake_pair):
            resp = await self.client.post("/api/pair", json={"to": PEER})
            body = await resp.json()
        self.assertEqual(resp.status, 200, body)
        self.assertTrue(body["paired"])
        self.assertEqual(body["peer"], PEER)
        self.assertEqual(self.calls, [("10.0.0.2", "fp-me", PEER, 8891)])
        self.assertTrue(self.store.is_paired(PEER))

    async def test_rejected_reply_is_not_a_pair(self):
        async def fake_pair(*args, **kwargs):
            return 302, "{302: device not match}"

        with mock.patch.object(channel, "pair_device", fake_pair):
            resp = await self.client.post("/api/pair", json={"to": PEER})
            body = await resp.json()
        self.assertEqual(resp.status, 400, body)
        self.assertFalse(body["ok"])
        self.assertEqual(body["replyCode"], 302)
        self.assertFalse(self.store.is_paired(PEER))

    async def test_unknown_device_is_404(self):
        resp = await self.client.post("/api/pair", json={"to": "nope-nope"})
        self.assertEqual(resp.status, 404)

    async def test_pair_repushes_groups_the_peer_belongs_to(self):
        from flixcli import groupchat
        self.store.create_group("g-1", "研发群")
        self.store.add_group_member("g-1", "fp-me")
        self.store.add_group_member("g-1", PEER)
        sent = []

        async def fake_pair(*args, **kwargs):
            return 0, "{0: success}"

        async def fake_post(ip, intent, port=8891, timeout=8.0):
            sent.append((ip, intent))
            return "{0: success}"

        with mock.patch.object(channel, "pair_device", fake_pair), \
                mock.patch.object(groupchat, "post_intent", fake_post):
            resp = await self.client.post("/api/pair", json={"to": PEER})
            body = await resp.json()
        self.assertEqual(resp.status, 200, body)
        syncs = [i for _ip, i in sent if i.get("action") == "rdGroupSync"]
        self.assertEqual(len(syncs), 1, sent)
        self.assertEqual(syncs[0]["extra"]["groupId"], "g-1")
        self.assertIn(PEER, syncs[0]["extra"]["members"])


if __name__ == "__main__":
    unittest.main()
