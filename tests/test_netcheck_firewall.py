"""netcheck 的防火墙覆盖率判定。

现场教训：规则「存在」不等于「覆盖当前网络」。一台机器上只有 Public 规则的
时候，netcheck 仍然显示 allowed: yes，而对端在 Private 的 Wi-Fi 上就是连不进来。
另外列举全部防火墙规则在规则多的机器上要几分钟，会超时并把结果变成 unknown，
所以改成按规则名查询。
"""
from __future__ import annotations

import unittest

from flixcli.cli import _firewall_coverage


class FirewallCoverageTests(unittest.TestCase):
    def test_public_only_rule_does_not_cover_private(self):
        got = _firewall_coverage(["Public"], ["Private"])
        self.assertEqual(got["covered"], [])
        self.assertEqual(got["uncovered"], ["Private"])

    def test_any_profile_covers_everything(self):
        got = _firewall_coverage(["Any"], ["Private"])
        self.assertEqual(got["uncovered"], [])

    def test_comma_separated_profiles_are_parsed(self):
        got = _firewall_coverage(["Domain, Private, Public"], ["Private"])
        self.assertEqual(got["covered"], ["private"])

    def test_no_rules_leaves_every_category_uncovered(self):
        got = _firewall_coverage([], ["Public", "Private"])
        self.assertEqual(sorted(got["uncovered"]), ["Private", "Public"])

    def test_case_insensitive_match(self):
        got = _firewall_coverage(["private"], ["Private"])
        self.assertEqual(got["uncovered"], [])

    def test_unknown_or_empty_profile_does_not_crash(self):
        got = _firewall_coverage(["", "Unknown"], ["Private"])
        self.assertEqual(got["uncovered"], ["Private"])


if __name__ == "__main__":
    unittest.main()
