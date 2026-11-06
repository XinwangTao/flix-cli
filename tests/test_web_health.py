"""Health probe payload used by deploy checks and monitoring."""
from __future__ import annotations

import unittest

from flixcli.webapp import health_payload


class HealthPayloadTests(unittest.TestCase):
    IDENT = {"alias": "board", "fingerprint": "abcd1234", "version": 1}

    def test_reports_identity_and_uptime(self):
        p = health_payload(self.IDENT, started_at=100.0, now=160.9,
                           device_count=3, channel_port=8891)
        self.assertTrue(p["ok"])
        self.assertEqual(p["alias"], "board")
        self.assertEqual(p["fingerprint"], "abcd1234")
        self.assertEqual(p["channelPort"], 8891)
        self.assertEqual(p["devices"], 3)
        self.assertEqual(p["uptime"], 60)

    def test_never_reports_negative_uptime(self):
        p = health_payload(self.IDENT, started_at=200.0, now=100.0,
                           device_count=0, channel_port=8891)
        self.assertEqual(p["uptime"], 0)

    def test_missing_identity_fields_default(self):
        p = health_payload({}, started_at=0.0, now=5.0, device_count=0,
                           channel_port=1)
        self.assertEqual(p["alias"], "")
        self.assertEqual(p["fingerprint"], "")


if __name__ == "__main__":
    unittest.main()
