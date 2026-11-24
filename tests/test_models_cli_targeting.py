"""`flix models stop|token` must only toggle the daemon that owns OUR port.

Ports are machine-wide, prefs are per-profile: without this guard, a CLI run
under a second profile switched off the first profile's gateway while reporting
its own port, and a foreground gateway on another port kept serving.
"""
from __future__ import annotations

import unittest
from unittest import mock

from flixcli import cli


def _status(**overrides) -> dict:
    base = {"ok": True, "enabled": True, "listening": True, "port": 18993,
            "token": "rd-x"}
    base.update(overrides)
    return base


class DaemonTargetingTests(unittest.TestCase):
    def test_same_port_daemon_is_used(self):
        with mock.patch.object(cli, "_desktop_api_request",
                               return_value=_status()):
            self.assertIsNotNone(cli._daemon_for_port(18998, 18993))

    def test_daemon_hosting_another_port_is_ignored(self):
        # The classic footgun: profile B's `models stop` switching off profile
        # A's gateway because it was the only daemon it could discover.
        with mock.patch.object(cli, "_desktop_api_request",
                               return_value=_status()):
            self.assertIsNone(cli._daemon_for_port(18998, 20993))

    def test_idle_daemon_may_still_be_enabled_for_any_port(self):
        with mock.patch.object(cli, "_desktop_api_request",
                               return_value=_status(listening=False, port=18993)):
            self.assertIsNotNone(cli._daemon_for_port(18998, 20993))

    def test_unreachable_or_broken_status_is_ignored(self):
        for payload in ({"ok": False, "error": "daemon-unreachable"},
                        {"ok": True, "port": "not-a-port", "listening": True},
                        "not-a-dict"):
            with mock.patch.object(cli, "_desktop_api_request",
                                   return_value=payload):
                self.assertIsNone(cli._daemon_for_port(18998, 18993), payload)


if __name__ == "__main__":
    unittest.main()
