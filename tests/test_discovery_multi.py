"""Discovery must cover every LAN interface, not just the default route."""
from __future__ import annotations

import unittest
from unittest import mock

from flixcli import discovery as disc


TABLE = [("192.168.237.7", "255.255.254.0"),
         ("192.168.137.1", "255.255.255.0"),
         ("10.10.200.68", "255.255.255.0")]


class MultiInterfaceTests(unittest.TestCase):
    def _discovery(self, iface=None):
        with mock.patch.object(disc, "local_ipv4_table", return_value=TABLE):
            return disc.Discovery(lambda dev: None, iface)

    def test_joins_every_usable_interface(self):
        d = self._discovery()
        self.assertEqual(d.ifaces, [ip for ip, _m in TABLE])

    def test_pinned_interface_wins(self):
        d = self._discovery("10.10.200.68")
        self.assertEqual(d.ifaces, ["10.10.200.68"])
        self.assertEqual(d.iface, "10.10.200.68")

    def test_broadcast_covers_each_interface(self):
        d = self._discovery()
        with mock.patch.object(disc, "local_ipv4_table", return_value=TABLE):
            addrs = d._broadcast_addrs()
        self.assertIn("192.168.137.255", addrs)
        self.assertIn("10.10.200.255", addrs)
        self.assertIn("255.255.255.255", addrs)
        self.assertEqual(addrs[-1], "255.255.255.255")

    def test_no_duplicate_broadcasts(self):
        table = [("192.168.137.1", "255.255.255.0"),
                 ("192.168.137.2", "255.255.255.0")]
        d = self._discovery()
        d.ifaces = [ip for ip, _m in table]
        with mock.patch.object(disc, "local_ipv4_table", return_value=table):
            addrs = d._broadcast_addrs()
        self.assertEqual(addrs.count("192.168.137.255"), 1)


if __name__ == "__main__":
    unittest.main()
