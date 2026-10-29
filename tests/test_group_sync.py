"""Group metadata snapshot (v2C): wire shape + last-writer-wins merge."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from flixcli import model, server
from flixcli.config import Store


class GroupSyncIntentTests(unittest.TestCase):
    def test_intent_shape_matches_android(self):
        intent = model.group_sync_intent("fp-me", "g-1", "研发群",
                                         ["fp-me", "fp-a"], 1234)
        self.assertEqual(intent["action"], model.ACTION_GROUP_SYNC)
        self.assertEqual(intent["deviceId"], "fp-me")
        self.assertEqual(intent["bubbleId"], "")
        self.assertEqual(intent["extra"], {
            "groupId": "g-1", "name": "研发群",
            "members": ["fp-me", "fp-a"], "rev": 1234,
        })

    def test_action_does_not_collide_with_official_actions(self):
        self.assertNotIn(model.ACTION_GROUP_SYNC, {
            model.ACTION_CLIPBOARD, model.ACTION_ASK_PAIR,
            model.ACTION_CONFIRM_PAIR, model.ACTION_CONFIRM_RECEIVE,
        })
        self.assertTrue(model.ACTION_GROUP_SYNC.startswith("rd"))


class GroupSyncMergeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "relaydesk.db")

    def tearDown(self):
        self.store.con.close()
        self._tmp.cleanup()

    def _group(self, gid: str = "g-1"):
        return [g for g in self.store.groups() if g["id"] == gid][0]

    def test_unknown_group_accepts_any_snapshot(self):
        self.assertEqual(self.store.group_rev("g-1"), -1)
        self.assertTrue(self.store.merge_group_sync("g-1", "旧名", ["a", "b"], 100))
        self.assertEqual(self.store.group_rev("g-1"), 100)
        self.assertEqual(self.store.group_members("g-1"), ["a", "b"])
        self.assertEqual(self._group()["name"], "旧名")

    def test_newer_rev_replaces_name_and_members(self):
        self.store.merge_group_sync("g-1", "旧名", ["a", "b"], 100)
        self.assertTrue(self.store.merge_group_sync("g-1", "新名", ["a", "c"], 200))
        self.assertEqual(self._group()["name"], "新名")
        self.assertEqual(self.store.group_members("g-1"), ["a", "c"])

    def test_equal_rev_keeps_local_state(self):
        self.store.merge_group_sync("g-1", "本地名", ["a"], 200)
        self.assertFalse(self.store.merge_group_sync("g-1", "对端名", ["z"], 200))
        self.assertEqual(self._group()["name"], "本地名")
        self.assertEqual(self.store.group_members("g-1"), ["a"])

    def test_older_rev_is_ignored(self):
        self.store.merge_group_sync("g-1", "当前名", ["a"], 300)
        self.assertFalse(self.store.merge_group_sync("g-1", "更旧", ["q"], 250))
        self.assertEqual(self._group()["name"], "当前名")

    def test_reopening_the_same_db_is_idempotent(self):
        path = Path(self._tmp.name) / "relaydesk.db"
        self.store.merge_group_sync("g-1", "群", ["a"], 10)
        self.store.con.close()
        # The rev ALTER runs again on every open; it must not raise.
        self.store = Store(path)
        self.assertEqual(self.store.group_rev("g-1"), 10)


class GroupSyncHttpTests(unittest.IsolatedAsyncioTestCase):
    """Drive the real /intent handler over aiohttp, as the Android peer does."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "relaydesk.db")
        self.store.add_pair("fp-peer", "1234")
        self.events: list[tuple[str, dict]] = []
        app = server.make_app(
            self.store, {"fingerprint": "fp-me", "alias": "me"},
            lambda name, payload: self.events.append((name, payload)))
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self._tmp.cleanup()

    async def _post(self, intent: dict):
        resp = await self.client.post("/intent", json=intent)
        return resp.status, (await resp.text()).strip()

    async def test_paired_peer_snapshot_is_applied(self):
        intent = model.group_sync_intent("fp-peer", "11111111-1111-4111-8111-111111111111", "研发群",
                                         ["fp-peer", "fp-me"], 500)
        self.assertEqual(await self._post(intent), (200, "{0: success}"))
        self.assertEqual(self.store.group_rev("11111111-1111-4111-8111-111111111111"), 500)
        self.assertEqual(set(self.store.group_members("11111111-1111-4111-8111-111111111111")),
                         {"fp-me", "fp-peer"})
        self.assertEqual(self.events, [("groupsChanged", {"groupId": "11111111-1111-4111-8111-111111111111"})])

    async def test_stale_snapshot_is_acked_but_ignored(self):
        self.store.merge_group_sync("11111111-1111-4111-8111-111111111111", "本地名", ["fp-me"], 900)
        intent = model.group_sync_intent("fp-peer", "11111111-1111-4111-8111-111111111111", "对端名",
                                         ["fp-peer"], 500)
        self.assertEqual(await self._post(intent), (200, "{0: success}"))
        self.assertEqual(self.store.group_rev("11111111-1111-4111-8111-111111111111"), 900)
        self.assertEqual(self.events, [])

    async def test_unpaired_sender_is_rejected(self):
        intent = model.group_sync_intent("fp-stranger", "11111111-1111-4111-8111-111111111111", "群",
                                         ["fp-stranger"], 100)
        self.assertEqual(await self._post(intent), (400, "receive intent failed"))
        self.assertEqual(self.store.group_rev("11111111-1111-4111-8111-111111111111"), -1)

    async def test_invalid_snapshots_are_rejected(self):
        bad_rev = model.group_sync_intent("fp-peer", "11111111-1111-4111-8111-111111111111", "群", ["fp-peer"], 0)
        no_members = model.group_sync_intent("fp-peer", "11111111-1111-4111-8111-111111111111", "群", [], 10)
        bad_gid = model.group_sync_intent("fp-peer", "not-a-uuid", "群",
                                          ["fp-peer"], 10)
        for intent in (bad_rev, no_members, bad_gid):
            self.assertEqual(await self._post(intent),
                             (400, "receive intent failed"))

    async def test_unrelated_actions_still_fall_through_to_500(self):
        # Official peers return 500 for unknown actions; keep that behaviour so
        # the rdGroupSync branch never widens what we accept.
        status, text = await self._post({"deviceId": "fp-peer",
                                         "action": "somethingElse",
                                         "extra": {}})
        self.assertEqual((status, text), (500, "receive intent failed"))


if __name__ == "__main__":
    unittest.main()
