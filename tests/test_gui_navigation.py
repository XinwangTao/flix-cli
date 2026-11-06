"""Navigation vs. polling.

A conversation being open must not pin the workspace to the transcript: the
2.5 s overview poll used to call `render_current()`, which switched the stack
back to the chat page, so settings/transfers/ADB were unreadable.
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
from PySide6.QtWidgets import QApplication  # noqa: E402

import flixcli.gui as gui  # noqa: E402

APP = QApplication.instance() or QApplication([])

gui.LocalApi.discover = staticmethod(lambda port=0: None)
gui.MainWindow.start_local_daemon = lambda self: None
gui.MainWindow._discovery_done = lambda self, found: None

ME = "fp-me"
PEER = "fp-peer"

CHAT_PAGE = 1
TRANSFERS_PAGE = 2
ADB_PAGE = 3
SETTINGS_PAGE = 4


def overview(alias: str = "TABLET") -> dict:
    return {
        "self": {"id": ME, "alias": "me", "port": 8898},
        "devices": [{"fingerprint": PEER, "alias": alias, "os": "win",
                     "ip": "10.0.0.2", "port": 8899}],
        "groups": [{"id": "g1", "name": "OPS", "members": [ME, PEER]}],
        "adb": {"devices": []},
        "media": {"sessions": []},
        "settings": {"saveDir": "D:/dl"},
        "bubbles": [
            {"id": "b1", "from": PEER, "to": ME, "type": 0,
             "time": 1_700_000_000_000, "groupId": "", "content": "hi"},
        ],
    }


def trigger_shortcut(window, sequence: str) -> None:
    """Fire a registered window shortcut.

    QTest.keyClick needs an active window, which the offscreen platform never
    provides, so the shortcut map is exercised through the action it holds.
    """
    for action in window.actions():
        if action.shortcut().toString() == sequence:
            action.trigger()
            return
    raise AssertionError(f"no action bound to {sequence}")


def make_window() -> gui.MainWindow:
    window = gui.MainWindow(19999)
    window.timer.stop()
    window.offline_mode = False
    window._auto_start_tried = True
    window.resize(1000, 620)
    window.self_id = ME
    window.payload = overview()
    window.apply_overview(overview())
    window.entity_list.setCurrentRow(0)  # open the conversation
    window.show()
    window.render_current()
    return window


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.window = make_window()

    def tearDown(self):
        self.window.close()

    def test_open_conversation_shows_transcript(self):
        self.assertEqual(self.window.workspace_stack.currentIndex(), CHAT_PAGE)

    def test_poll_does_not_yank_off_settings(self):
        self.window.set_mode("settings")
        self.assertEqual(self.window.workspace_stack.currentIndex(),
                         SETTINGS_PAGE)
        for alias in ("TABLET", "PHONE", "LAPTOP"):
            self.window.apply_overview(overview(alias))
            self.assertEqual(
                self.window.workspace_stack.currentIndex(), SETTINGS_PAGE,
                "poll snapped the reader back to the chat page")

    def test_poll_does_not_yank_off_transfers_or_adb(self):
        for mode, index in (("transfers", TRANSFERS_PAGE),
                            ("adb", ADB_PAGE),
                            ("settings", SETTINGS_PAGE)):
            self.window.set_mode(mode)
            self.window.apply_overview(overview())
            self.assertEqual(self.window.workspace_stack.currentIndex(), index)

    def test_poll_still_refreshes_header_while_reading_chat(self):
        self.window.apply_overview(overview("RENAMED"))
        self.assertEqual(self.window.workspace_stack.currentIndex(), CHAT_PAGE)
        self.assertEqual(self.window.session_title.text(), "RENAMED")

    def test_returning_to_devices_shows_transcript_again(self):
        self.window.set_mode("settings")
        self.window.payload["bubbles"].append(
            {"id": "b2", "from": ME, "to": PEER, "type": 0,
             "time": 1_700_000_060_000, "groupId": "", "content": "while away"})
        self.window.apply_overview(self.window.payload)
        self.window.set_mode("devices")
        self.assertEqual(self.window.workspace_stack.currentIndex(), CHAT_PAGE)
        self.assertIn("b2", self.window._timeline_ids)

    def test_rail_click_from_settings_opens_conversation(self):
        self.window.set_mode("settings")
        self.window.entity_list.setCurrentRow(-1)
        self.window.current = {"kind": "device", "key": PEER}
        self.window.set_mode("devices")
        self.assertEqual(self.window.workspace_stack.currentIndex(), CHAT_PAGE)

    # ---------- page chrome ----------

    def test_side_page_owns_the_header(self):
        self.window.set_mode("settings")
        self.assertEqual(self.window.session_title.text(), "设置与状态")
        self.assertFalse(self.window.header_toolbar.isVisible())
        self.assertFalse(self.window.session_search.isVisible())

    def test_side_page_hides_actions_that_would_hit_a_hidden_chat(self):
        self.window.set_mode("transfers")
        self.assertFalse(self.window.header_toolbar.isVisible())
        self.assertTrue(self.window.header_toolbar.isEnabled())

    def test_returning_to_chat_restores_header_and_toolbar(self):
        self.window.set_mode("settings")
        self.window.set_mode("devices")
        self.assertEqual(self.window.session_title.text(), "TABLET")
        self.assertTrue(self.window.header_toolbar.isVisible())
        self.assertTrue(self.window.session_search.isVisible())

    def test_sidebar_keeps_conversations_on_side_pages(self):
        self.window.set_mode("settings")
        titles = [self.window.entity_list.item(i).data(Qt.UserRole)["title"]
                  for i in range(self.window.entity_list.count())]
        self.assertIn("TABLET", titles)
        self.assertIn("OPS", titles)

    def test_sidebar_click_from_settings_opens_chat_with_toolbar(self):
        self.window.set_mode("settings")
        self.assertFalse(self.window.header_toolbar.isVisible())
        self.window.entity_list.setCurrentRow(-1)  # polling restored row 0 already
        self.window.entity_list.setCurrentRow(0)
        self.assertEqual(self.window.workspace_stack.currentIndex(), CHAT_PAGE)
        self.assertTrue(self.window.header_toolbar.isVisible())
        self.assertEqual(self.window.session_title.text(), "TABLET")
        # The rail follows the pick instead of still pointing at 设置.
        checked = [b.property("mode") for b in self.window.rail_buttons
                   if b.isChecked()]
        self.assertEqual(checked, ["devices"])

    def test_poll_keeps_side_page_header(self):
        self.window.set_mode("settings")
        self.window.apply_overview(overview("RENAMED"))
        self.assertEqual(self.window.session_title.text(), "设置与状态")

    def test_escape_returns_from_side_page_to_chat(self):
        self.window.set_mode("settings")
        trigger_shortcut(self.window, "Esc")
        self.assertEqual(self.window.workspace_stack.currentIndex(), CHAT_PAGE)

    def test_ctrl_number_jumps_between_sections(self):
        trigger_shortcut(self.window, "Ctrl+6")
        self.assertEqual(self.window.workspace_stack.currentIndex(), SETTINGS_PAGE)
        trigger_shortcut(self.window, "Ctrl+1")
        self.assertEqual(self.window.workspace_stack.currentIndex(), CHAT_PAGE)


if __name__ == "__main__":
    unittest.main()
