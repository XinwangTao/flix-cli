"""Target resolution: an `ip:port` must pick the device on THAT port.

Field case: `flix shell 192.168.237.200:18991` answered "not-paired" although
the device is paired on :18991 — discovery had left a placeholder row for the
official client's :8891 next to it, and matching on the bare address returned
the placeholder first.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flixcli.config import Store
from flixcli.routing import resolve_target

IP = "192.168.237.200"
PAIRED_FP = "d8c6383e-65a9-48d8-82b2-82afc57b0fde"
OFFICIAL_FP = "bfd8a976-42cb-4a9e-92ee-15cbe07331a9"


class ResolveTargetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "db.sqlite")
        # Order matters: placeholders sort first (alias = the address itself).
        self.store.upsert_device(f"ip:{IP}:8891", alias=IP, ip=IP, port=8891)
        self.store.upsert_device(f"ip:{IP}:18991", alias="DESKTOP-K087HI9",
                                 ip=IP, port=18991)
        self.store.upsert_device(OFFICIAL_FP, alias="DESKTOP-K087HI9", ip=IP,
                                 port=8891)
        self.store.upsert_device(PAIRED_FP, alias="DESKTOP-K087HI9", ip=IP,
                                 port=18991)
        self.store.add_pair(PAIRED_FP, "1234")
        self.store.add_pair(OFFICIAL_FP, "1234")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_explicit_port_picks_the_record_on_that_port(self):
        ip, port, fp = resolve_target(self.store, f"{IP}:18991")
        self.assertEqual((ip, port, fp), (IP, 18991, PAIRED_FP))
        self.assertTrue(self.store.is_paired(fp))

    def test_explicit_port_of_the_official_client_still_resolves(self):
        ip, port, fp = resolve_target(self.store, f"{IP}:8891")
        self.assertEqual((ip, port), (IP, 8891))
        self.assertEqual(fp, OFFICIAL_FP)

    def test_bare_ip_prefers_a_paired_device_over_a_placeholder(self):
        ip, port, fp = resolve_target(self.store, IP)
        self.assertEqual(ip, IP)
        self.assertTrue(self.store.is_paired(fp), fp)
        self.assertIn(port, (8891, 18991))

    def test_unknown_address_is_passed_through_unchanged(self):
        ip, port, fp = resolve_target(self.store, "10.0.0.5:19999")
        self.assertEqual((ip, port, fp), ("10.0.0.5", 19999, "10.0.0.5:19999"))

    def test_fingerprint_and_prefix_win_over_the_address_table(self):
        ip, port, fp = resolve_target(self.store, "d8c6383e")
        self.assertEqual((ip, fp), (IP, PAIRED_FP))
        self.assertEqual(port, 18991)


if __name__ == "__main__":
    unittest.main()
