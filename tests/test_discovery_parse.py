"""Announce parsing must not let malformed fields reach the device table.

``record_announce`` treats the parsed dict as trusted: it keys the table by the
fingerprint and routes to the announced port. A non-string fingerprint or a
port outside 1-65535 used to be stored as-is and only failed much later.
"""
from __future__ import annotations

import json
import unittest

from flixcli import DEVICE_PORT
from flixcli.discovery import build_announce, parse_datagram


def datagram(drop: tuple[str, ...] = (), **over) -> bytes:
    modal = {"alias": "board", "deviceModel": "Linux", "deviceType": "desktop",
             "fingerprint": "peer-fp", "port": 18991, "version": 1,
             "ip": "", "host": "", "from": "", "os": "linux"}
    for key in drop:
        modal.pop(key, None)
    modal.update(over)
    inner = {"deviceModal": modal}
    return json.dumps(json.dumps(inner, separators=(",", ":")),
                      separators=(",", ":")).encode("utf-8")


def build_modal(ident: dict, **kwargs) -> dict:
    return json.loads(json.loads(build_announce(ident, **kwargs)))["deviceModal"]


class BuildAnnounceTests(unittest.TestCase):
    def test_long_fields_are_bounded_before_sending(self):
        modal = build_modal({"alias": "a" * 500, "deviceModel": "m" * 200,
                             "os": "o" * 100, "fingerprint": "peer-fp"})
        self.assertEqual((len(modal["alias"]), len(modal["deviceModel"]),
                          len(modal["os"])), (128, 64, 32))

    def test_invalid_port_falls_back_before_sending(self):
        modal = build_modal({"fingerprint": "peer-fp"}, port=70000)
        self.assertEqual(modal["port"], DEVICE_PORT)
        self.assertEqual(build_modal({"fingerprint": "peer-fp"},
                                     port=18991)["port"], 18991)

    def test_defaults_and_non_string_fields(self):
        modal = build_modal({"fingerprint": "peer-fp", "alias": 123})
        self.assertEqual(modal["alias"], "123")
        self.assertEqual(modal["deviceModel"], "Windows")
        self.assertEqual(modal["os"], "windows")


class ParseDatagramTests(unittest.TestCase):
    def test_valid_announce_is_normalized(self):
        dev = parse_datagram(datagram(), "10.0.0.9")
        self.assertEqual((dev["fingerprint"], dev["port"], dev["ip"],
                          dev["source"]),
                         ("peer-fp", 18991, "10.0.0.9", "10.0.0.9"))

    def test_missing_zero_or_out_of_range_port_uses_the_default(self):
        for over in ({"port": None}, {"port": 0}, {"port": -1},
                     {"port": 70000}, {"port": "not-a-port"}, {"port": True}):
            with self.subTest(port=over["port"]):
                dev = parse_datagram(datagram(**over), "10.0.0.9")
                self.assertEqual(dev["port"], DEVICE_PORT)
        dev = parse_datagram(datagram(drop=("port",)), "10.0.0.9")
        self.assertEqual(dev["port"], DEVICE_PORT)

    def test_numeric_string_port_is_coerced(self):
        self.assertEqual(parse_datagram(datagram(port="8891"), "10.0.0.9")["port"],
                         8891)

    def test_fingerprint_must_be_a_nonempty_string(self):
        for over in ({"fingerprint": 123}, {"fingerprint": {}},
                     {"fingerprint": "   "}, {"fingerprint": None}):
            with self.subTest(fingerprint=over["fingerprint"]):
                self.assertIsNone(parse_datagram(datagram(**over), "10.0.0.9"))
        self.assertIsNone(parse_datagram(datagram(drop=("fingerprint",)),
                                         "10.0.0.9"))

    def test_whitespace_fingerprint_is_trimmed(self):
        dev = parse_datagram(datagram(fingerprint=" peer-fp "), "10.0.0.9")
        self.assertEqual(dev["fingerprint"], "peer-fp")

    def test_fingerprint_length_is_bounded(self):
        self.assertEqual(
            parse_datagram(datagram(fingerprint="f" * 128), "10.0.0.9")
            ["fingerprint"], "f" * 128)
        self.assertIsNone(parse_datagram(datagram(fingerprint="f" * 129),
                                         "10.0.0.9"))

    def test_modal_ip_is_kept_but_source_is_the_packet_peer(self):
        dev = parse_datagram(datagram(ip="192.168.1.5"), "10.0.0.9")
        self.assertEqual((dev["ip"], dev["source"]), ("192.168.1.5", "10.0.0.9"))

    def test_single_encoded_json_is_accepted(self):
        modal = {"deviceModal": {"fingerprint": "peer-fp", "port": 18991}}
        dev = parse_datagram(json.dumps(modal).encode(), "10.0.0.9")
        self.assertEqual(dev["fingerprint"], "peer-fp")

    def test_long_display_fields_are_truncated(self):
        dev = parse_datagram(
            datagram(alias="a" * 500, deviceModel="m" * 200, os="o" * 100),
            "10.0.0.9")
        self.assertEqual(
            (len(dev["alias"]), len(dev["deviceModel"]), len(dev["os"])),
            (128, 64, 32))

    def test_display_fields_are_coerced_to_strings(self):
        dev = parse_datagram(datagram(alias=123, deviceModel=None, os=["x"]),
                             "10.0.0.9")
        self.assertEqual(dev["alias"], "123")
        self.assertEqual(dev["deviceModel"], "")
        self.assertEqual(dev["os"], "['x']")

    def test_invalid_json_and_wrong_shapes_return_none(self):
        for blob in (b"not json", b"[]", b'{"deviceModal": "x"}',
                     b'{"deviceModal": {"port": 1}}'):
            with self.subTest(blob=blob):
                self.assertIsNone(parse_datagram(blob, "10.0.0.9"))


if __name__ == "__main__":
    unittest.main()
