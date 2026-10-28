"""Focused tests for local group mention routing."""
from __future__ import annotations

import unittest

from flixcli.groupchat import _mention_targets


class GroupMentionTests(unittest.TestCase):
    MEMBERS = ["aabbccdd-1111", "aabbccdd-2222", "ffeeddcc-3333"]

    def test_mention_targets_selects_matching_prefix(self):
        self.assertEqual(_mention_targets(self.MEMBERS, "@ffeeddcc please check"),
                         ["ffeeddcc-3333"])

    def test_multiple_mentions_preserve_member_order(self):
        self.assertEqual(_mention_targets(self.MEMBERS, "@ffeeddcc @aabbccdd now"),
                         self.MEMBERS)

    def test_unknown_mention_keeps_broadcast_behavior(self):
        self.assertEqual(_mention_targets(self.MEMBERS, "@missing hello"), self.MEMBERS)

    def test_plain_text_keeps_broadcast_behavior(self):
        self.assertEqual(_mention_targets(self.MEMBERS, "hello everyone"), self.MEMBERS)


if __name__ == "__main__":
    unittest.main()
