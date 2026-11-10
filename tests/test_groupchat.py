"""Focused tests for local group mention routing and cross-node group state."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from flixcli import cli, groupchat
from flixcli.config import Store
from flixcli.groupchat import _mention_targets

ME = "fp-me"
PEER = "fp-peer"


class GroupMentionTests(unittest.TestCase):
    MEMBERS = ["aabbccdd-1111", "aabbccdd-2222", "ffeeddcc-3333"]

    def test_mention_targets_selects_matching_prefix(self):
        self.assertEqual(_mention_targets(self.MEMBERS, "@ffeeddcc please check"),
                         ["ffeeddcc-3333"])

    def test_multiple_mentions_preserve_member_order(self):
        self.assertEqual(_mention_targets(self.MEMBERS, "@ffeeddcc @aabbccdd now"),
                         self.MEMBERS)

    def test_unknown_mention_keeps_broadcast_behavior(self):
        self.assertEqual(_mention_targets(self.MEMBERS, "@missing hello"), self.MEMBERS)

    def test_plain_text_keeps_broadcast_behavior(self):
        self.assertEqual(_mention_targets(self.MEMBERS, "hello everyone"), self.MEMBERS)


if __name__ == "__main__":
    unittest.main()


class AdoptGroupTests(unittest.TestCase):
    """A bubble tagged with an unknown groupId must not vanish behind no-group."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "relaydesk.db")
        self.gid = str(uuid.uuid5(uuid.NAMESPACE_OID, "board-group"))

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_paired_sender_registers_placeholder_group(self):
        self.store.add_pair(PEER, "k")
        self.assertTrue(groupchat.adopt_group(self.store, {"fingerprint": ME},
                                              self.gid, PEER))
        group = [g for g in self.store.groups() if g["id"] == self.gid][0]
        self.assertEqual(group["members"], [ME, PEER])
        self.assertEqual(self.store.group_rev(self.gid), 0)
        # the thread becomes reachable instead of answering "no-group"
        self.store.add_text_bubble("b-1", PEER, ME, "群里的第一条", 1, self.gid)
        self.assertEqual(len(self.store.group_history(self.gid)), 1)

    def test_unpaired_sender_cannot_conjure_a_group(self):
        self.assertFalse(groupchat.adopt_group(self.store, {"fingerprint": ME},
                                               self.gid, PEER))
        self.assertEqual(self.store.groups(), [])

    def test_known_group_is_left_alone(self):
        self.store.add_pair(PEER, "k")
        self.store.merge_group_sync(self.gid, "研发群", [ME, PEER, "fp-c"], 500)
        self.assertFalse(groupchat.adopt_group(self.store, {"fingerprint": ME},
                                               self.gid, PEER))
        self.assertEqual(self.store.groups()[0]["name"], "研发群")
        self.assertEqual(len(self.store.group_members(self.gid)), 3)

    def test_real_snapshot_replaces_the_placeholder(self):
        self.store.add_pair(PEER, "k")
        groupchat.adopt_group(self.store, {"fingerprint": ME}, self.gid, PEER)
        self.assertTrue(self.store.merge_group_sync(
            self.gid, "研发群", [ME, PEER, "fp-c"], 1793983000000))
        self.assertEqual(self.store.groups()[0]["name"], "研发群")
        self.assertEqual(len(self.store.group_members(self.gid)), 3)

    def test_rejects_non_uuid_and_repeats(self):
        self.store.add_pair(PEER, "k")
        self.assertFalse(groupchat.adopt_group(self.store, {"fingerprint": ME},
                                               "g-1", PEER))
        self.assertTrue(groupchat.adopt_group(self.store, {"fingerprint": ME},
                                              self.gid, PEER))
        self.assertFalse(groupchat.adopt_group(self.store, {"fingerprint": ME},
                                               self.gid, PEER))


class CliGroupPushTests(unittest.TestCase):
    """`flix group create/add` must ship rdGroupSync like the GUI does."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "relaydesk.db"
        self.store = Store(self.path)
        self.store.upsert_device(PEER, alias="board", port=8891, ip="10.0.0.2",
                                 os_name="linux")
        self.store.con.commit()
        self.sent = []
        self._orig_post = groupchat.post_intent

        async def fake_post(ip, intent, port=8891, timeout=8.0):
            self.sent.append((ip, intent))
            return "{0: success}"

        groupchat.post_intent = fake_post
        ident = lambda _store: {"fingerprint": ME, "alias": "me"}  # noqa: E731
        # cmd_* opens its own Store and closes it, and groupchat.new_group
        # resolves the local identity through the config module: patch both.
        self._patches = [
            mock.patch.object(cli, "Store", lambda *a, **k: Store(self.path)),
            mock.patch.object(cli, "load_or_create_identity", ident),
            mock.patch("flixcli.config.load_or_create_identity", ident),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        groupchat.post_intent = self._orig_post
        self.store.close()
        self._tmp.cleanup()

    def _run(self, coro):
        return asyncio.run(coro)

    def _fresh(self):
        return Store(self.path)

    def test_create_pushes_snapshot_to_members(self):
        args = type("A", (), {"name": "研发群", "member": [PEER], "json": False})()
        self.assertEqual(self._run(cli.cmd_group_create(args)), 0)
        self.assertEqual(len(self.sent), 1)
        ip, intent = self.sent[0]
        self.assertEqual(ip, "10.0.0.2")
        extra = intent["extra"]
        self.assertEqual(extra["name"], "研发群")
        self.assertEqual(sorted(extra["members"]), sorted([ME, PEER]))
        self.assertGreater(extra["rev"], 0)
        store = self._fresh()
        try:
            self.assertEqual(store.group_rev(extra["groupId"]), extra["rev"])
        finally:
            store.close()

    def test_add_pushes_membership_instead_of_only_writing_locally(self):
        gid = str(uuid.uuid4())
        self.store.create_group(gid, "研发群")
        self.store.add_group_member(gid, ME)
        args = type("A", (), {"group": gid, "fingerprint": PEER, "json": False})()
        self.assertEqual(self._run(cli.cmd_group_add(args)), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn(PEER, self.sent[0][1]["extra"]["members"])
        store = self._fresh()
        try:
            self.assertIn(PEER, store.group_members(gid))
        finally:
            store.close()

    def test_unknown_group_still_answers_no_group(self):
        args = type("A", (), {"group": "missing", "fingerprint": PEER, "json": True})()
        self.assertEqual(self._run(cli.cmd_group_add(args)), 2)
        self.assertEqual(self.sent, [])
