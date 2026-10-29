"""Copying from the transcript: selection survives polls, and copy works.

The old renderer tore the transcript down on every 2.5 s poll, which killed
any selection mid-drag — Ctrl+A / Ctrl+C / right-click copy all seemed broken.
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

import flixcli.gui as gui  # noqa: E402

APP = QApplication.instance() or QApplication([])

PEER = "fp-peer"
ME = "fp-me"
BUBBLES = [
    {"id": "b1", "from": PEER, "to": ME, "type": 0, "time": 1_700_000_000_000,
     "groupId": "", "content": "第一行\n第二行"},
    {"id": "b2", "from": ME, "to": PEER, "type": 0, "time": 1_700_000_060_000,
     "groupId": "", "content": "收到"},
]


def make_window() -> gui.MainWindow:
    window = gui.MainWindow(19999)  # nothing listening: refresh just fails
    window.timer.stop()
    window.self_id = ME
    window.payload = {"bubbles": BUBBLES, "devices": [{"fingerprint": PEER, "alias": "TABLET"}]}
    window.current = {"kind": "device", "key": PEER}
    window._timeline_signature = None
    window.show()          # offscreen, but selection needs a laid-out widget
    window.render_timeline()
    return window


def bubble_labels(window) -> list[QLabel]:
    return [w for w in window.timeline_widget.findChildren(QLabel)
            if w.objectName() == "bubbleText"]


def label_with(window, needle: str) -> QLabel:
    for label in bubble_labels(window):
        if needle in label.text():
            return label
    raise AssertionError(f"no bubble containing {needle!r}")


class CopyTests(unittest.TestCase):
    def setUp(self):
        self.window = make_window()

    def tearDown(self):
        self.window.close()

    def test_bubbles_are_selectable_by_keyboard(self):
        labels = bubble_labels(self.window)
        self.assertEqual(len(labels), 2)
        for label in labels:
            self.assertTrue(label.textInteractionFlags() & gui.Qt.TextSelectableByMouse)
            self.assertTrue(label.textInteractionFlags() & gui.Qt.TextSelectableByKeyboard)

    def test_selection_is_reported(self):
        label = label_with(self.window, "第一行")
        label.setSelection(0, 3)
        self.assertTrue(self.window.timeline_selection().startswith("第一行"))

    def test_render_is_skipped_when_nothing_changed(self):
        before = bubble_labels(self.window)
        self.window.render_timeline()
        after = bubble_labels(self.window)
        self.assertEqual([id(w) for w in before], [id(w) for w in after],
                         "identical frames must not rebuild the transcript")

    def test_live_selection_blocks_a_rebuild(self):
        label = label_with(self.window, "第一行")
        label.setSelection(0, 3)
        fresh = dict(BUBBLES[0], id="b3", time=1_700_000_120_000, content="新消息")
        self.window.payload = {"bubbles": [fresh] + BUBBLES,
                               "devices": self.window.payload["devices"]}
        before = [id(w) for w in bubble_labels(self.window)]
        self.window.render_timeline()
        self.assertEqual([id(w) for w in bubble_labels(self.window)], before,
                         "a rebuild while selecting would drop the highlight")

    def test_copy_all_writes_the_whole_conversation(self):
        self.window.copy_all_messages()
        text = QApplication.clipboard().text()
        self.assertIn("TABLET：第一行", text)
        self.assertIn("我：收到", text)

    def test_copy_all_uses_file_names(self):
        self.window.payload["bubbles"] = BUBBLES + [
            {"id": "b4", "from": PEER, "to": ME, "type": 3, "time": 1_700_000_180_000,
             "groupId": "", "content": {"name": "photo.png"}}]
        self.window.copy_all_messages()
        self.assertIn("[文件] photo.png", QApplication.clipboard().text())


if __name__ == "__main__":
    unittest.main()
