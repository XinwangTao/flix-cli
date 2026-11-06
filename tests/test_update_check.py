"""启动时的版本检查：结果结构与 /healthz 透出。"""
from __future__ import annotations

import unittest
from unittest import mock

from flixcli import autoupdate
from flixcli.webapp import health_payload


class CheckUpdateTests(unittest.TestCase):
    def _release(self, tag):
        return autoupdate.Release(tag=tag, name=tag, prerelease=False,
                                  draft=False, url="https://example/" + tag)

    def test_reports_newer_release(self):
        with mock.patch.object(autoupdate, "fetch_releases", return_value=[self._release("v9.9.9")]):
            info = autoupdate.check_update(current="2.2.1")
        self.assertTrue(info["ok"])
        self.assertTrue(info["available"])
        self.assertEqual(info["latest"], "9.9.9")
        self.assertEqual(info["current"], "2.2.1")

    def test_same_or_older_is_not_available(self):
        with mock.patch.object(autoupdate, "fetch_releases", return_value=[self._release("v2.2.1")]):
            self.assertFalse(autoupdate.check_update(current="2.2.1")["available"])
        with mock.patch.object(autoupdate, "fetch_releases", return_value=[self._release("v2.0.0")]):
            self.assertFalse(autoupdate.check_update(current="2.2.1")["available"])

    def test_network_error_degrades_quietly(self):
        err = autoupdate.UpdateError("network", "timeout")
        with mock.patch.object(autoupdate, "fetch_releases", side_effect=err):
            info = autoupdate.check_update(current="2.2.1")
        self.assertFalse(info["ok"])
        self.assertEqual(info["error"], "network")
        self.assertFalse(info.get("available", False))


class HealthPayloadUpdateTests(unittest.TestCase):
    def test_health_exposes_update_state(self):
        ident = {"alias": "board", "fingerprint": "abc", "version": 1}
        info = {"ok": True, "current": "2.2.1", "latest": "2.3.0", "available": True}
        payload = health_payload(ident, 0.0, 5.0, 1, 8891, update=info)
        self.assertEqual(payload["update"]["latest"], "2.3.0")

    def test_health_without_update_is_null(self):
        ident = {"alias": "board", "fingerprint": "abc", "version": 1}
        self.assertIsNone(health_payload(ident, 0.0, 5.0, 1, 8891)["update"])


if __name__ == "__main__":
    unittest.main()
