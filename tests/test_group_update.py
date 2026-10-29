"""Desktop group management (v2I): local snapshot first, then per-member push."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from flixcli import groupchat
from flixcli.channel import PeerError
from flixcli.config import Store

ME = "fp-me"


def run(coro):
    return asyncio.run(coro)


class UpdateGroupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "relaydesk.db")
        self.store.create_group("g-1", "研发群")
        for fp in (ME, "fp-a", "fp-b"):
            self.store.add_group_member("g-1", fp)
        for fp, ip in ((ME, "10.0.0.1"), ("fp-a", "10.0.0.2"), ("fp-b", "10.0.0.3")):
            self.store.con.execute(
                "INSERT INTO persistence_devices (fingerprint, alias, device_model,"
                " device_type, port, version, ip, host, \"from\", insert_or_update_time, os)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (fp, fp, "Windows", None, 8891, 1, ip, "", "", 1, "windows"))
        self.store.con.commit()
        self.sent = []
        self._orig_post = groupchat.post_intent

        async def fake_post(ip, intent, port=8891, timeout=8.0):
            if ip == "10.0.0.3":
                raise PeerError("rejected", "HTTP 500 receive intent failed")
            self.sent.append((ip, intent))
            return "{0: success}"

        groupchat.post_intent = fake_post

    def tearDown(self):
        groupchat.post_intent = self._orig_post
        self.store.con.close()
        self._tmp.cleanup()

    def test_rename_writes_locally_and_reports_failures(self):
        result = run(groupchat.update_group(
            self.store, {"fingerprint": ME}, "g-1", name="  新名字  "))
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "新名字")
        # local state first: the change survives even though one peer refused
        self.assertEqual(self.store.groups()[0]["name"], "新名字")
        self.assertEqual(self.store.group_rev("g-1"), result["rev"])
        # one target ok, one refused, self never addressed
        self.assertEqual([r["to"] for r in result["results"]], ["fp-a", "fp-b"])
        self.assertEqual(result["delivered"], 1)
        self.assertEqual(result["total"], 2)
        self.assertEqual(self.sent[0][0], "10.0.0.2")
        self.assertEqual(self.sent[0][1]["extra"]["name"], "新名字")

    def test_add_and_remove_members(self):
        result = run(groupchat.update_group(
            self.store, {"fingerprint": ME}, "g-1",
            add=("fp-c",), remove=("fp-b",)))
        self.assertTrue(result["ok"])
        self.assertEqual(sorted(self.store.group_members("g-1")),
                         sorted([ME, "fp-a", "fp-c"]))

    def test_cannot_remove_self(self):
        before = self.store.group_rev("g-1")
        result = run(groupchat.update_group(
            self.store, {"fingerprint": ME}, "g-1", remove=(ME,)))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "self-removed")
        self.assertEqual(self.store.group_rev("g-1"), before)
        self.assertIn(ME, self.store.group_members("g-1"))

    def test_unknown_group(self):
        result = run(groupchat.update_group(
            self.store, {"fingerprint": ME}, "nope", name="x"))
        self.assertEqual(result["error"], "no-group")


if __name__ == "__main__":
    unittest.main()
