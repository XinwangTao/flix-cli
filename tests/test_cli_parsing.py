"""CLI parsing: nested verbs must accept --json after the verb too.

`flix group send --json` always worked, while `flix adb report --json` and
`flix call list --json` answered with a usage error — people type the flag last,
so the nested parsers get a shared parent instead.
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "flixcli.cli", *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=180)


class NestedVerbFlagTests(unittest.TestCase):
    def _assert_parsed(self, *args: str) -> subprocess.CompletedProcess:
        result = _run(*args)
        self.assertNotIn("unrecognized arguments", result.stderr,
                         f"{args} 被 argparse 拒绝：{result.stderr.strip()}")
        self.assertNotEqual(result.returncode, 2,
                            f"{args} 以用法错误退出：{result.stderr.strip()}")
        return result

    def test_adb_report_accepts_json_after_the_verb(self):
        result = self._assert_parsed("adb", "report", "--json")
        self.assertTrue(result.stdout.strip().startswith("{"),
                        f"不是 JSON 输出：{result.stdout[:120]!r}")
        json.loads(result.stdout)

    def test_adb_keeps_accepting_json_before_the_verb(self):
        result = self._assert_parsed("adb", "--json", "report")
        self.assertTrue(result.stdout.strip().startswith("{"))

    def test_adb_devices_accepts_json_after_the_verb(self):
        result = self._assert_parsed("adb", "devices", "--json")
        self.assertTrue(result.stdout.strip().startswith("{"))

    def test_call_list_accepts_json_after_the_verb(self):
        # Without a daemon it still answers JSON (daemon-unreachable), which is
        # enough to prove the flag parsed instead of erroring out.
        result = self._assert_parsed("call", "list", "--json")
        self.assertTrue(result.stdout.strip().startswith("{"))

    def test_adb_report_without_json_stays_text(self):
        result = self._assert_parsed("adb", "report")
        text = result.stdout.strip()
        # A host without adb prints nothing at all (CI has no Android SDK), so
        # only assert the shape: the default output is never the JSON document.
        self.assertFalse(text.startswith("{"),
                         f"默认不该输出 JSON：{text[:120]!r}")


if __name__ == "__main__":
    unittest.main()
