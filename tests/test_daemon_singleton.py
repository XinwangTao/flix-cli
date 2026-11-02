"""Single-instance lock, cross-process prefs safety, and upgrade script gates."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from flixcli.upgrade import build_script


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DaemonSingletonTests(unittest.TestCase):
    def test_second_daemon_refuses_to_share_the_app_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata = Path(tmp) / "appdata"
            appdata.mkdir()
            dev_port, api_port = _free_port(), _free_port()
            env = {**os.environ, "APPDATA": str(appdata)}
            cmd = [sys.executable, "-m", "flixcli.daemon",
                   "--device-port", str(dev_port),
                   "--api-port", str(api_port),
                   "--no-announce", "--no-clip-sync"]
            first = subprocess.Popen(cmd, env=env,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, text=True)
            try:
                up = False
                for _ in range(60):
                    try:
                        with urllib.request.urlopen(
                                f"http://127.0.0.1:{api_port}/api/self",
                                timeout=1):
                            up = True
                            break
                    except OSError:
                        time.sleep(0.25)
                self.assertTrue(up, "first daemon did not start")

                second = subprocess.run(cmd, env=env, capture_output=True,
                                        text=True, timeout=30)
                self.assertEqual(second.returncode, 5)
                self.assertIn("already running", second.stderr)
            finally:
                first.terminate()
                try:
                    first.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    first.kill()


class PrefsConcurrencyTests(unittest.TestCase):
    def test_parallel_writers_do_not_drop_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata = Path(tmp) / "appdata"
            appdata.mkdir()
            env = {**os.environ, "APPDATA": str(appdata)}
            writers = [
                subprocess.Popen(
                    [sys.executable, "-c",
                     "from flixcli.config import Store;"
                     f"Store().set_pref('k{i}', 'v{i}')"],
                    env=env, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
                for i in range(8)
            ]
            for p in writers:
                self.assertEqual(p.wait(timeout=60), 0)
            prefs = json.loads(
                (appdata / "flixcli" / "shared_preferences.json")
                .read_text(encoding="utf-8"))
            for i in range(8):
                self.assertEqual(prefs.get(f"k{i}"), f"v{i}")


class UpgradeScriptTests(unittest.TestCase):
    def test_script_uses_real_save_dir_and_verifies_hash(self):
        script = build_script("pkg.exe", "stamp", "log.txt", 8891, 8898, 30,
                              sha256="A" * 64)
        self.assertIn("saveDir", script)
        self.assertIn("Get-FileHash", script)
        self.assertIn("A" * 64, script)
        self.assertIn("ExecutablePath", script)
        self.assertNotIn("RelayDesk-v2.2.1-win64", script)

    def test_failed_rollback_does_not_start_a_broken_exe(self):
        script = build_script("pkg.exe", "s", "l", 8891, 8898, 10,
                              sha256="B" * 64)
        self.assertIn("FAIL rollback", script)
        self.assertIn("-ErrorAction Stop", script)


if __name__ == "__main__":
    unittest.main()
