"""Transcript scroll policy.

Opening a conversation lands on the newest message; scrolling up must stay
where the reader put it (new messages only raise the "new messages" pill).
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import PySide6  # noqa: F401
except ImportError:  # GUI 是可选依赖，基础环境（CI）里跳过
    raise unittest.SkipTest("PySide6 not installed")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import flixcli.gui as gui  # noqa: E402

APP = QApplication.instance() or QApplication([])

ME = "fp-me"
PEER = "fp-peer"
OTHER = "fp-other"


def bubbles_for(peer: str, count: int = 40) -> list[dict]:
    return [
        {"id": f"{peer}-{i}", "from": peer if i % 2 else ME, "to": peer,
         "type": 0, "time": 1_700_000_000_000 + i * 60_000, "groupId": "",
         "content": f"{i:02d} " + "这是一条用来撑满时间线的测试消息。" * 3}
        for i in range(count)
    ]


def make_window() -> gui.MainWindow:
    window = gui.MainWindow(19999)  # nothing listening: refresh just fails
    window.timer.stop()
    window.resize(1000, 620)
    window.self_id = ME
    window.payload = {
        "bubbles": bubbles_for(PEER) + bubbles_for(OTHER),
        "devices": [{"fingerprint": PEER, "alias": "TABLET"},
                    {"fingerprint": OTHER, "alias": "PHONE"}],
        "groups": [],
    }
    window.current = {"kind": "device", "key": PEER}
    window._timeline_signature = None
    window.show()
    window.render_current()   # puts the chat page on the stack, then renders
    QTest.qWait(150)
    return window


class ScrollPolicyTests(unittest.TestCase):
    def setUp(self):
        self.window = make_window()
        self.bar = self.window.timeline.verticalScrollBar()

    def tearDown(self):
        self.window.close()

    def assert_at_tail(self):
        self.assertGreater(self.bar.maximum(), 0)
        self.assertEqual(self.bar.value(), self.bar.maximum())

    def test_open_conversation_lands_on_newest(self):
        self.assert_at_tail()

    def test_switching_conversation_lands_on_newest(self):
        self.bar.setValue(0)
        QTest.qWait(30)
        self.window.current = {"kind": "device", "key": OTHER}
        self.window.render_current()
        QTest.qWait(150)
        self.assert_at_tail()

    def test_scroll_up_is_not_yanked_by_new_message(self):
        self.bar.setValue(0)
        QTest.qWait(30)
        before = self.bar.value()
        self.window.payload["bubbles"].append(
            {"id": "new-1", "from": PEER, "to": ME, "type": 0,
             "time": 1_700_000_900_000, "groupId": "", "content": "新消息"})
        self.window.render_timeline()
        QTest.qWait(150)
        self.assertEqual(self.bar.value(), before)
        self.assertLess(self.bar.value(), self.bar.maximum())
        self.assertTrue(self.window.new_msg_btn.isVisible())
        self.assertIn("新消息", self.window.new_msg_btn.text())

    def test_reclick_open_conversation_returns_to_newest(self):
        self.bar.setValue(0)
        QTest.qWait(30)

        class Item:
            def data(self, _role):
                return {"kind": "device", "key": PEER}

        self.window._on_entity_clicked(Item())
        QTest.qWait(150)
        self.assert_at_tail()
        self.assertFalse(self.window.new_msg_btn.isVisible())

    def test_clicking_the_sidebar_row_returns_to_newest(self):
        self.window.populate_sidebar()
        QTest.qWait(30)
        item = next(self.window.entity_list.item(i)
                    for i in range(self.window.entity_list.count())
                    if (self.window.entity_list.item(i).data(Qt.UserRole) or
                        {}).get("key") == PEER)
        rect = self.window.entity_list.visualItemRect(item)

        QTest.mouseClick(self.window.entity_list.viewport(), Qt.LeftButton,
                         pos=rect.center())
        QTest.qWait(150)
        self.assert_at_tail()

        self.bar.setValue(0)
        QTest.qWait(30)
        QTest.mouseClick(self.window.entity_list.viewport(), Qt.LeftButton,
                         pos=rect.center())
        QTest.qWait(150)
        self.assert_at_tail()


    def test_append_keeps_the_reader_anchored(self):
        self.bar.setValue(self.bar.maximum() // 2)
        QTest.qWait(30)
        anchor_key, anchor_offset = self.window._scroll_anchor()
        widget = self.window._timeline_rows[anchor_key]
        self.assertEqual(
            widget.y() - self.bar.value(), anchor_offset,
            f"anchor={anchor_key} y={widget.y()} value={self.bar.value()} "
            f"max={self.bar.maximum()} follow={self.window._follow_tail} "
            f"user={self.window._user_scrolled}")

        self.window.payload["bubbles"].insert(0, {
            "id": "new-tail", "from": PEER, "to": ME, "type": 0,
            "time": 1_700_000_900_000, "groupId": "", "content": "新消息"})
        self.window.render_timeline()
        QTest.qWait(450)

        widget = self.window._timeline_rows[anchor_key]
        self.assertEqual(
            widget.y() - self.bar.value(), anchor_offset,
            f"anchor={anchor_key} y={widget.y()} value={self.bar.value()} "
            f"max={self.bar.maximum()} follow={self.window._follow_tail} "
            f"user={self.window._user_scrolled}")

    def test_status_change_remounts_only_the_changed_row(self):
        files = []
        for i in range(6):
            files.append({
                "id": f"f{i}", "from": PEER if i % 2 else ME, "to": PEER,
                "type": 3, "time": 1_700_000_000_000 + i * 60_000,
                "groupId": "",
                "content": {"name": f"file-{i}.bin", "size": 1024,
                            "state": 1, "progress": 0.1},
            })
        self.window.payload = {"bubbles": list(reversed(files)),
                               "devices": [{"fingerprint": PEER, "alias": "TABLET"}]}
        self.window._timeline_signature = None
        self.window.render_timeline()
        QTest.qWait(150)
        before = {key: id(row) for key, row in self.window._timeline_rows.items()}
        self.assertIn("msg:f3", before,
                      f"rows={sorted(before)} sel={self.window.timeline_selection()!r} "
                      f"rebuild={self.window._timeline_rebuilding}")

        files[3]["content"]["progress"] = 0.7
        self.window.payload = {"bubbles": list(reversed(files)),
                               "devices": self.window.payload["devices"]}
        self.window.render_timeline()
        QTest.qWait(450)
        after = {key: id(row) for key, row in self.window._timeline_rows.items()}

        self.assertNotEqual(after["msg:f3"], before["msg:f3"])
        for key in before:
            if key != "msg:f3":
                self.assertEqual(after[key], before[key], key)


    def test_capped_history_drops_only_the_oldest_row(self):
        base = 1_700_000_000_000
        bubbles = [{"id": f"m{i}", "from": PEER if i % 2 else ME, "to": PEER,
                    "type": 0, "time": base + (100 - i) * 60_000, "groupId": "",
                    "content": f"消息 {i}"} for i in range(100)]  # newest first
        devices = [{"fingerprint": PEER, "alias": "TABLET"}]
        self.window.payload = {"bubbles": bubbles, "devices": devices}
        self.window._timeline_signature = None
        self.window.render_timeline()
        QTest.qWait(150)
        before = {key: id(row) for key, row in self.window._timeline_rows.items()}

        self.bar.setValue(self.bar.maximum() // 2)
        QTest.qWait(30)
        anchor_key, anchor_offset = self.window._scroll_anchor()

        newest = {"id": "m100", "from": PEER, "to": ME, "type": 0,
                  "time": base + 1_000_000, "groupId": "", "content": "最新消息"}
        self.window.payload = {"bubbles": [newest] + bubbles[:-1],
                               "devices": devices}
        self.window.render_timeline()
        QTest.qWait(450)
        after = {key: id(row) for key, row in self.window._timeline_rows.items()}

        self.assertNotIn("msg:m99", after)
        self.assertIn("msg:m100", after)
        for key in before:
            if key != "msg:m99":
                self.assertEqual(after[key], before[key],
                                 f"{key} must keep its widget")
        widget = self.window._timeline_rows[anchor_key]
        self.assertEqual(
            widget.y() - self.bar.value(), anchor_offset,
            f"anchor={anchor_key} y={widget.y()} value={self.bar.value()} "
            f"max={self.bar.maximum()} follow={self.window._follow_tail} "
            f"user={self.window._user_scrolled}")


if __name__ == "__main__":
    unittest.main()
