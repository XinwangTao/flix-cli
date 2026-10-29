"""Presence TTL rules: stored devices keep an IP forever, so online means
"contacted inside PRESENCE_TTL_MS", not "has an IP"."""
from __future__ import annotations

import unittest

from flixcli.presence import PRESENCE_TTL_MS, is_online


class PresenceTests(unittest.TestCase):
    def test_fresh_device_is_online(self):
        now = 1_800_000_000_000
        device = {"ip": "192.168.1.20", "seenAt": now - 1_000}
        self.assertTrue(is_online(device, now))

    def test_stale_device_is_offline(self):
        now = 1_800_000_000_000
        device = {"ip": "192.168.1.20", "seenAt": now - PRESENCE_TTL_MS - 1}
        self.assertFalse(is_online(device, now))

    def test_boundary_is_inclusive(self):
        now = 1_800_000_000_000
        device = {"ip": "192.168.1.20", "seenAt": now - PRESENCE_TTL_MS}
        self.assertTrue(is_online(device, now))

    def test_missing_ip_is_offline(self):
        now = 1_800_000_000_000
        self.assertFalse(is_online({"seenAt": now}, now))
        self.assertFalse(is_online({"ip": "", "seenAt": now}, now))

    def test_missing_or_invalid_seen_at_is_offline(self):
        now = 1_800_000_000_000
        self.assertFalse(is_online({"ip": "10.0.0.2"}, now))
        self.assertFalse(is_online({"ip": "10.0.0.2", "seenAt": 0}, now))
        self.assertFalse(is_online({"ip": "10.0.0.2", "seenAt": "yesterday"}, now))

    def test_empty_device_is_offline(self):
        self.assertFalse(is_online({}, 1_800_000_000_000))


if __name__ == "__main__":
    unittest.main()
