"""Polling must not rebuild the widget tree when the data is unchanged.

Every 2.5 s overview poll used to clear the sidebar and recreate each row,
forcing a whole-window repaint that made the DWM glass material flicker.
"""
from __future__ import annotations

import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    from flixcli import gui
except ImportError:  # pragma: no cover - bare CI box
    gui = None


@unittest.skipIf(gui is None, "PySide6 is not installed")
class SidebarRefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = gui.MainWindow()
        self.window.payload = {
            "devices": [{"alias": "peer", "fingerprint": "fp-1", "os": "linux",
                         "ip": "10.0.0.2", "port": 8891,
                         "seenAt": int(time.time() * 1000)}],
            "groups": [],
        }

    def tearDown(self):
        self.window.close()

    def _rows(self):
        widget_list = self.window.entity_list
        return [widget_list.itemWidget(widget_list.item(index))
                for index in range(widget_list.count())]

    def test_unchanged_payload_keeps_the_same_rows(self):
        self.window.populate_sidebar()
        first = self._rows()
        self.assertEqual(len(first), 1)
        self.window.populate_sidebar()
        second = self._rows()
        self.assertIs(first[0], second[0])

    def test_changed_alias_rebuilds_the_rows(self):
        self.window.populate_sidebar()
        first = self._rows()
        self.window.payload["devices"][0]["alias"] = "renamed"
        self.window.populate_sidebar()
        second = self._rows()
        self.assertIsNot(first[0], second[0])


if __name__ == "__main__":
    unittest.main()
