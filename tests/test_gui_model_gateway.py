"""The GUI must configure the gateway in the *daemon's* prefs file.

Regression: the settings page wrote modelGateway into ~/.relaydesk_gui.json
while `flix models start` / the daemon read shared_preferences.json, so the
token shown in the UI never matched the running gateway (phones got 401).
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import PySide6  # noqa: F401
except ImportError:
    raise unittest.SkipTest("PySide6 not installed")

import flixcli.gui as gui  # noqa: E402
from flixcli import config  # noqa: E402


class ModelGatewayConfigSourceTests(unittest.TestCase):
    def test_gui_store_points_at_the_daemon_prefs(self):
        store = gui.MainWindow._model_store(None)
        try:
            self.assertEqual(Path(store.prefs_path), Path(config.PREFS_PATH))
        finally:
            store.close()

    def test_token_written_by_one_store_is_seen_by_the_next(self):
        from flixcli.model_gateway import load_config, save_config

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite"
            first = config.Store(db)
            try:
                token = load_config(first)["token"]
            finally:
                first.close()
            second = config.Store(db)
            try:
                self.assertEqual(load_config(second)["token"], token)
                save_config(second, enabled=True)
            finally:
                second.close()
            third = config.Store(db)
            try:
                cfg = load_config(third)
                self.assertEqual(cfg["token"], token)
                self.assertTrue(cfg["enabled"])
            finally:
                third.close()


if __name__ == "__main__":
    unittest.main()
