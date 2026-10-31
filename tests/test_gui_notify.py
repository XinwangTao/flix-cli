"""Desktop toast decisions — pure functions, no Qt window involved."""
from __future__ import annotations

import unittest

try:
    import PySide6  # noqa: F401
except ImportError:  # GUI 是可选依赖，基础环境（CI）里跳过
    raise unittest.SkipTest("PySide6 not installed")

from flixcli.gui import (
    TOAST_GAP, TOAST_H, TOAST_MARGIN, TOAST_W, notification_text, pick_notification,
    toast_position,
)

PEER_MSG = {"id": "b1", "from": "fp-peer", "to": "fp-me", "type": 0,
            "time": 2, "groupId": "", "content": "在吗"}
MINE = {"id": "b2", "from": "fp-me", "to": "fp-peer", "type": 0,
        "time": 3, "groupId": "", "content": "在"}
DEVICES = [{"fingerprint": "fp-peer", "alias": "TABLET"}]


def pick(bubbles, previous, *, active=False, enabled=True, self_id="fp-me"):
    return pick_notification(bubbles, previous, self_id,
                             active=active, enabled=enabled)


class PickNotificationTests(unittest.TestCase):
    def test_first_poll_only_seeds(self):
        self.assertIsNone(pick([PEER_MSG], None))

    def test_known_bubble_is_quiet(self):
        self.assertIsNone(pick([PEER_MSG], {"b1"}))

    def test_new_incoming_toasts(self):
        fresh = dict(PEER_MSG, id="b3", time=4)
        self.assertEqual(pick([PEER_MSG, fresh], {"b1"})["id"], "b3")

    def test_own_messages_never_toast(self):
        self.assertIsNone(pick([MINE], {"b1"}))

    def test_pref_off_silences(self):
        fresh = dict(PEER_MSG, id="b9")
        self.assertIsNone(pick([fresh], {"b1"}, enabled=False))

    def test_active_window_silences(self):
        fresh = dict(PEER_MSG, id="b8")
        self.assertIsNone(pick([fresh], {"b1"}, active=True))

    def test_latest_of_several_is_reported(self):
        a = dict(PEER_MSG, id="b4", time=10)
        b = dict(PEER_MSG, id="b5", time=20)
        self.assertEqual(pick([a, b], {"b1"})["id"], "b5")


class NotificationTextTests(unittest.TestCase):
    def test_text_uses_alias(self):
        title, body = notification_text(PEER_MSG, DEVICES)
        self.assertEqual(title, "TABLET")
        self.assertEqual(body, "在吗")

    def test_group_is_named_in_title(self):
        bubble = dict(PEER_MSG, groupId="g-1")
        self.assertEqual(notification_text(bubble, DEVICES)[0], "TABLET · 群消息")

    def test_file_bubble_shows_name(self):
        bubble = {"id": "b6", "from": "fp-peer", "type": 3, "groupId": "",
                  "content": {"name": "photo.png", "size": 10}}
        self.assertEqual(notification_text(bubble, DEVICES)[1], "▤ photo.png")

    def test_unknown_sender_falls_back_to_prefix(self):
        bubble = dict(PEER_MSG, **{"from": "zzzzzzzz-1111"})
        self.assertEqual(notification_text(bubble, DEVICES)[0], "zzzzzzzz")

    def test_whitespace_is_collapsed(self):
        bubble = dict(PEER_MSG, content="第一行\n\n第二行")
        self.assertEqual(notification_text(bubble, DEVICES)[1], "第一行 第二行")


class ToastPositionTests(unittest.TestCase):
    AREA = (0, 0, 1920, 1040)  # primary screen minus the taskbar

    def test_first_toast_sits_in_the_bottom_right(self):
        x, y = toast_position(self.AREA, 0)
        self.assertEqual(x, 1920 - TOAST_W - TOAST_MARGIN)
        self.assertEqual(y, 1040 - TOAST_H - TOAST_MARGIN)

    def test_each_further_toast_stacks_upward(self):
        _, y0 = toast_position(self.AREA, 0)
        _, y1 = toast_position(self.AREA, 1)
        self.assertEqual(y0 - y1, TOAST_H + TOAST_GAP)

    def test_offsets_follow_the_screen_origin(self):
        off = (300, 200, 1280, 720)
        x, y = toast_position(off, 0)
        self.assertEqual(x, 300 + 1280 - TOAST_W - TOAST_MARGIN)
        self.assertEqual(y, 200 + 720 - TOAST_H - TOAST_MARGIN)


if __name__ == "__main__":
    unittest.main()
