"""Target resolution: fingerprint, IP and IP:port all name the same peer.

The IP form used to come back with the literal address as the peer id. Every
command that consults the local pair table (``flix shell``, GUI pair, group
membership) then treated a paired board as unpaired, and bubbles carried an IP
in ``to``/``deviceId`` where the wire protocol expects a fingerprint.
"""
from __future__ import annotations

import unittest

from flixcli.routing import resolve_target


class _Store:
    def __init__(self, rows):
        self._rows = rows

    def devices(self):
        return list(self._rows)


ROWS = [
    {"fingerprint": "364220a6-a616-4b0c-bf57-d3050e1bdcc9", "ip": "192.168.137.162",
     "port": 8891, "seenAt": 1_000},
    {"fingerprint": "4c4ca286-cd87-4439-b67a-f0d1f3664371", "ip": "192.168.137.1",
     "port": 8891, "seenAt": 2_000},
]


class ResolveTargetTests(unittest.TestCase):
    def test_fingerprint_exact_and_prefix(self):
        self.assertEqual(resolve_target(_Store(ROWS), ROWS[0]["fingerprint"]),
                         ("192.168.137.162", 8891, ROWS[0]["fingerprint"]))
        self.assertEqual(resolve_target(_Store(ROWS), "364220a6"),
                         ("192.168.137.162", 8891, ROWS[0]["fingerprint"]))

    def test_ip_resolves_to_stored_fingerprint(self):
        self.assertEqual(resolve_target(_Store(ROWS), "192.168.137.162"),
                         ("192.168.137.162", 8891, ROWS[0]["fingerprint"]))

    def test_ip_with_explicit_port_keeps_the_port(self):
        self.assertEqual(resolve_target(_Store(ROWS), "192.168.137.162:18991"),
                         ("192.168.137.162", 18991, ROWS[0]["fingerprint"]))

    def test_unknown_ip_still_sends_directly(self):
        self.assertEqual(resolve_target(_Store(ROWS), "10.0.0.9"),
                         ("10.0.0.9", None, "10.0.0.9"))

    def test_duplicate_ip_prefers_freshest_device(self):
        rows = ROWS + [{"fingerprint": "new-device", "ip": "192.168.137.162",
                        "port": 8891, "seenAt": 3_000}]
        self.assertEqual(resolve_target(_Store(rows), "192.168.137.162")[2],
                         "new-device")

    def test_duplicate_ip_with_port_prefers_that_instance(self):
        rows = ROWS + [{"fingerprint": "hook", "ip": "192.168.137.162",
                        "port": 18991, "seenAt": 500}]
        self.assertEqual(resolve_target(_Store(rows), "192.168.137.162:18991")[2],
                         "hook")

    def test_unknown_name_is_not_a_device(self):
        self.assertEqual(resolve_target(_Store(ROWS), "no-such-host"),
                         (None, None, "no-such-host"))

    def test_row_without_ip_is_skipped(self):
        rows = [{"fingerprint": "abc-123", "ip": "", "port": 8891,
                 "seenAt": 1}]
        self.assertEqual(resolve_target(_Store(rows), "abc"), (None, None, "abc"))


if __name__ == "__main__":
    unittest.main()
