"""Prefs must follow the database, or tests rewrite the real install's settings.

`tests/test_transfer_security.py` writes a temp saveDir and a 32-byte receive
cap; while prefs were global those values landed in the live install and made
every incoming file fail silently.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flixcli.config import PREFS_PATH, Store
from flixcli.server import DEFAULT_MAX_FILE_BYTES, _max_file_bytes


class PrefsIsolationTests(unittest.TestCase):
    def test_temp_store_writes_prefs_beside_its_own_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite")
            try:
                store.set_pref("saveDir", str(root / "downloads"))
                self.assertEqual(store.prefs()["saveDir"], str(root / "downloads"))
                local = root / "shared_preferences.json"
                self.assertTrue(local.exists(), "prefs should live next to the db")
            finally:
                store.close()

    def test_temp_store_does_not_touch_the_live_prefs_file(self):
        before = PREFS_PATH.read_bytes() if PREFS_PATH.exists() else None
        real_mtime = PREFS_PATH.stat().st_mtime_ns if PREFS_PATH.exists() else None
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            try:
                store.set_pref("saveDir", "X:/never-used")
                store.set_pref("max_receive_bytes", 32)
            finally:
                store.close()
        after = PREFS_PATH.read_bytes() if PREFS_PATH.exists() else None
        self.assertEqual(before, after)
        if real_mtime is not None:
            self.assertEqual(real_mtime, PREFS_PATH.stat().st_mtime_ns)

    def test_default_store_keeps_using_the_install_prefs(self):
        store = Store()
        try:
            self.assertEqual(store.prefs_path, PREFS_PATH)
        finally:
            store.close()


class ReceiveCapTests(unittest.TestCase):
    def _cap(self, value):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            try:
                if value is not None:
                    store.set_pref("max_receive_bytes", value)
                return _max_file_bytes(store)
            finally:
                store.close()

    def test_zero_means_no_explicit_cap(self):
        self.assertEqual(self._cap(0), DEFAULT_MAX_FILE_BYTES)

    def test_absent_means_default(self):
        self.assertEqual(self._cap(None), DEFAULT_MAX_FILE_BYTES)

    def test_explicit_cap_is_honoured(self):
        self.assertEqual(self._cap(1024), 1024)

    def test_oversized_cap_clamps_to_default(self):
        self.assertEqual(self._cap(DEFAULT_MAX_FILE_BYTES * 4), DEFAULT_MAX_FILE_BYTES)


if __name__ == "__main__":
    unittest.main()