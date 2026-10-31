"""Device list dedupe: one physical machine must show up once.

A host is recorded once per fingerprint, but the official app (8891) and
RelayDesk (18991) use different fingerprints, and an older install of this
machine can announce itself as a peer — device pickers then list the same PC
several times.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flixcli.config import Store


class VisibleDeviceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _seed(self, fingerprint, alias, ip, port, seen_at):
        self.store.upsert_device(fingerprint, alias=alias, ip=ip, port=port)
        self.store.con.execute(
            "UPDATE persistence_devices SET insert_or_update_time=? WHERE fingerprint=?",
            (seen_at, fingerprint))
        self.store.con.commit()

    def test_same_host_with_two_fingerprints_keeps_the_freshest(self):
        self._seed("old-fp", "DESKTOP", "192.168.1.20", 8891, 1_000)
        self._seed("new-fp", "DESKTOP", "192.168.1.20", 18991, 2_000)
        rows = self.store.visible_devices(own="me", local_ips=["192.168.1.99"])
        self.assertEqual([r["fingerprint"] for r in rows], ["new-fp"])

    def test_own_fingerprint_and_own_addresses_are_hidden(self):
        self._seed("me", "SELF", "192.168.1.99", 8891, 3_000)
        self._seed("me-old", "SELF", "192.168.1.99", 8891, 1_000)
        self._seed("peer", "PEER", "192.168.1.5", 8891, 2_000)
        rows = self.store.visible_devices(own="me", local_ips=["192.168.1.99"])
        self.assertEqual([r["fingerprint"] for r in rows], ["peer"])

    def test_distinct_addresses_stay_separate(self):
        self._seed("a", "TABLET", "192.168.1.5", 8891, 1_000)
        self._seed("b", "PHONE", "192.168.1.6", 8891, 1_000)
        rows = self.store.visible_devices(own="me", local_ips=[])
        self.assertEqual(sorted(r["fingerprint"] for r in rows), ["a", "b"])

    def test_rows_without_ip_are_still_listed(self):
        self.store.upsert_device("a", alias="A")
        self.store.upsert_device("b", alias="B")
        rows = self.store.visible_devices(own="me", local_ips=[])
        self.assertEqual(sorted(r["fingerprint"] for r in rows), ["a", "b"])

    def test_raw_devices_keeps_every_fingerprint_for_routing(self):
        self._seed("old-fp", "DESKTOP", "192.168.1.20", 8891, 1_000)
        self._seed("new-fp", "DESKTOP", "192.168.1.20", 18991, 2_000)
        self.assertEqual(len(self.store.devices()), 2)


if __name__ == "__main__":
    unittest.main()
