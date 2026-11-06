"""Announce is a heartbeat: presence must not depend on TCP reachability.

A peer behind a firewall may hear our discovery datagrams (and push to us) while
our /ping_v2 probe can never open its inbound 8891. Presence used to be
refreshed only by a successful probe, so such a peer dropped out of the 90s TTL
while it was still announcing every 30s.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from flixcli.config import Store
from flixcli.daemon_runtime import record_announce
from flixcli.discovery import EndpointTracker
from flixcli.presence import PRESENCE_TTL_MS, is_online

ME = "me-0000-1111-2222-333333333333"
PEER = "peer-0000-1111-2222-333333333333"


def _announce(**over):
    dev = {"alias": "board", "deviceModel": "Linux", "deviceType": "desktop",
           "fingerprint": PEER, "port": 8891, "version": 1, "os": "linux",
           "ip": "192.168.137.162", "source": "192.168.137.162"}
    dev.update(over)
    return dev


class RecordAnnounceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite")
        self.ident = {"fingerprint": ME}
        self.devices: dict[str, dict] = {}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _seen(self, fp=PEER):
        row = self.store.con.execute(
            "SELECT insert_or_update_time FROM persistence_devices"
            " WHERE fingerprint=?", (fp,)).fetchone()
        return row[0] if row else None

    def _age(self, fp, ms):
        self.store.con.execute(
            "UPDATE persistence_devices SET insert_or_update_time=?"
            " WHERE fingerprint=?", (int(time.time() * 1000) - ms, fp))
        self.store.con.commit()

    def test_new_announce_is_reported_once(self):
        self.assertTrue(record_announce(self.store, self.ident, self.devices,
                                        _announce()))
        self.assertFalse(record_announce(self.store, self.ident, self.devices,
                                         _announce()),
                         "identical announce must not churn the UI/log")
        row = self.store.devices()[0]
        self.assertEqual((row["alias"], row["ip"], row["port"]),
                         ("board", "192.168.137.162", 8891))

    def test_repeat_announce_keeps_a_firewalled_peer_online(self):
        self.assertTrue(record_announce(self.store, self.ident, self.devices,
                                        _announce()))
        # Two minutes without a single successful /ping_v2: the old code left
        # seenAt here and the device read offline after 90s.
        self._age(PEER, 120_000)
        self.assertFalse(is_online(self.store.devices()[0]))
        self.assertFalse(record_announce(self.store, self.ident, self.devices,
                                         _announce()))
        self.assertTrue(is_online(self.store.devices()[0]),
                        "repeat announce must refresh seenAt")

    def test_touch_is_throttled_across_a_burst(self):
        record_announce(self.store, self.ident, self.devices, _announce())
        self._age(PEER, 5_000)          # inside the 15s throttle window
        stale = self._seen()
        record_announce(self.store, self.ident, self.devices, _announce())
        self.assertEqual(self._seen(), stale,
                         "burst copies must not write the row every datagram")

    def test_changed_modal_still_counts_as_a_new_sighting(self):
        record_announce(self.store, self.ident, self.devices,
                        _announce(port=8891))
        self.assertTrue(record_announce(self.store, self.ident, self.devices,
                                        _announce(alias="renamed")))
        self.assertEqual(self.store.devices()[0]["alias"], "renamed")

    def test_multiple_live_ports_keep_the_default_endpoint(self):
        """Same identity on 8891 and 18991 must not rewrite the port each burst."""
        endpoints = EndpointTracker()
        self.assertTrue(record_announce(self.store, self.ident, self.devices,
                                        _announce(port=18991), endpoints))
        self.assertTrue(record_announce(self.store, self.ident, self.devices,
                                        _announce(port=8891), endpoints))
        self.assertEqual(self.store.devices()[0]["port"], 8891)
        # The 18991 instance announces again 30s later: it is a heartbeat, not
        # a device change, and routing stays on the protocol default port.
        self.assertFalse(record_announce(self.store, self.ident, self.devices,
                                         _announce(port=18991), endpoints))
        self.assertEqual(self.store.devices()[0]["port"], 8891)
        self.assertFalse(record_announce(self.store, self.ident, self.devices,
                                         _announce(port=8891), endpoints))
        self.assertEqual(self.store.devices()[0]["port"], 8891)

    def test_endpoint_tracker_switches_only_after_the_ttl(self):
        tracker = EndpointTracker(ttl_ms=90_000)
        t0 = 1_000_000
        tracker.remember(PEER, "192.168.137.1", 18991, t0)
        tracker.remember(PEER, "192.168.137.1", 8891, t0)
        self.assertEqual(tracker.select(PEER, "192.168.137.1", t0 + 1),
                         ("192.168.137.1", 8891))
        # 8891 stopped announcing but is still inside the TTL: no flapping.
        self.assertEqual(tracker.select(PEER, "192.168.137.1", t0 + 60_000),
                         ("192.168.137.1", 8891))
        # 18991 kept announcing; once 8891 expires, the survivor takes over.
        tracker.remember(PEER, "192.168.137.1", 18991, t0 + 60_000)
        self.assertEqual(tracker.select(PEER, "192.168.137.1", t0 + 100_000),
                         ("192.168.137.1", 18991))
        self.assertIsNone(tracker.select(PEER, "10.0.0.9", t0 + 100_000))

    def test_endpoint_selection_is_scoped_to_the_announced_ip(self):
        tracker = EndpointTracker(ttl_ms=PRESENCE_TTL_MS)
        tracker.remember(PEER, "192.168.137.1", 8891, 1_000)
        tracker.remember(PEER, "10.0.0.9", 18991, 1_000)
        self.assertEqual(tracker.select(PEER, "10.0.0.9", 1_100),
                         ("10.0.0.9", 18991))
        self.assertEqual(tracker.select(PEER, "192.168.137.1", 1_100),
                         ("192.168.137.1", 8891))

    def test_own_and_fingerprintless_announces_are_ignored(self):
        self.assertFalse(record_announce(self.store, self.ident, self.devices,
                                         _announce(fingerprint=ME)))
        self.assertFalse(record_announce(self.store, self.ident, self.devices,
                                         {"alias": "x", "ip": "10.0.0.9"}))
        self.assertEqual(self.store.devices(), [])


if __name__ == "__main__":
    unittest.main()
