"""Remote upgrade: package gate and the generated PowerShell template."""
from __future__ import annotations

import os
import tempfile
import unittest

from flixcli.upgrade import build_script, verify_package


class VerifyPackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.exe = os.path.join(self.tmp.name, "RelayDesk.exe")
        with open(self.exe, "wb") as fh:
            fh.write(b"MZ" + b"\x00" * 64)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rejects_non_exe(self):
        with self.assertRaisesRegex(ValueError, "only .exe"):
            verify_package(os.path.join(self.tmp.name, "notes.txt"))

    def test_rejects_missing_file(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            verify_package(os.path.join(self.tmp.name, "nope.exe"))

    def test_reports_size_and_hash(self):
        info = verify_package(self.exe)
        self.assertEqual(info["size"], 66)
        self.assertTrue(info["sha256"].isupper())
        self.assertEqual(len(info["sha256"]), 64)


class BuildScriptTests(unittest.TestCase):
    def test_no_placeholders_left(self):
        script = build_script("RelayDesk-upgrade-x.exe", "20261102-090000",
                              "rd_upgrade.log", 18991, 18998, 60)
        self.assertFalse([p for p in ("__SRC__", "__STAMP__", "__PORT__",
                                      "__API__", "__TRIES__", "__LOG__",
                                      "__FRESH__") if p in script])

    def test_rollback_restores_the_backup(self):
        script = build_script("RelayDesk-upgrade-x.exe", "s1", "l.log",
                              18991, 18998, 60)
        self.assertIn("unhealthy-rollback", script)
        self.assertIn("Copy-Item -LiteralPath $backup", script)

    def test_fresh_env_is_opt_in(self):
        default = build_script("a.exe", "s", "l", 18991, 18998, 60)
        fresh = build_script("a.exe", "s", "l", 18991, 18998, 60, fresh_env=True)
        self.assertIn("$env:FLIX_UPGRADE_FRESH_ENV = '0'", default)
        self.assertIn("$env:FLIX_UPGRADE_FRESH_ENV = '1'", fresh)
        # 默认不清解包变量：实测有机器清掉后新进程起不来
        self.assertIn("Remove-Item Env:_PYI_APPLICATION_HOME_DIR", fresh)


if __name__ == "__main__":
    unittest.main()
