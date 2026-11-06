"""Clipboard backends: Windows, Linux GUI helpers and the headless spool."""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from flixcli import clipboard
from flixcli.webapp import health_payload


def linux(**env):
    return types.SimpleNamespace(platform="linux")


class BackendSelectionTests(unittest.TestCase):
    def test_windows_needs_no_session_env(self):
        with mock.patch.object(clipboard, "sys", types.SimpleNamespace(platform="win32")):
            self.assertEqual(clipboard.backend(), "win32")

    def test_wayland_wins_over_x11(self):
        env = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}
        with mock.patch.object(clipboard, "sys", linux()), \
                mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(clipboard.shutil, "which",
                                  lambda n: "/usr/bin/" + n):
            self.assertEqual(clipboard.backend(), "wl-clipboard")

    def test_x11_falls_back_to_xsel(self):
        env = {"DISPLAY": ":0"}
        with mock.patch.object(clipboard, "sys", linux()), \
                mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(clipboard.shutil, "which",
                                  lambda n: "/usr/bin/" + n
                                  if n == "xsel" else None):
            self.assertEqual(clipboard.backend(), "xsel")

    def test_headless_board_has_no_backend(self):
        with mock.patch.object(clipboard, "sys", linux()), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(clipboard.shutil, "which", lambda n: "/usr/bin/" + n):
            self.assertEqual(clipboard.backend(), "")


class WriteTests(unittest.TestCase):
    def test_headless_write_spools_private_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp) / "state" / "clipboard.txt"
            with mock.patch.object(clipboard, "sys", linux()), \
                    mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.object(clipboard.shutil, "which", lambda n: None):
                sink = clipboard.write_text("板子剪贴板 ✓", spool)
            self.assertEqual(sink, "file:%s" % spool)
            self.assertEqual(spool.read_text(encoding="utf-8"), "板子剪贴板 ✓")
            # Windows has no POSIX permission bits; os.chmod only maps the
            # read-only flag there, so the 0600 assertion is Linux-only.
            if os.name == "posix":
                mode = stat.S_IMODE(spool.stat().st_mode)
                self.assertEqual(mode, 0o600)

    def test_headless_write_without_spool_reports_failure(self):
        with mock.patch.object(clipboard, "sys", linux()), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(clipboard.shutil, "which", lambda n: None):
            self.assertEqual(clipboard.write_text("x"), "")

    def test_xclip_receives_utf8_body(self):
        calls = []

        def fake_run(argv, data=None, timeout=2.0):
            calls.append((argv, data))
            return types.SimpleNamespace(returncode=0, stdout=b"")

        env = {"DISPLAY": ":0"}
        with mock.patch.object(clipboard, "sys", linux()), \
                mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(clipboard.shutil, "which",
                                  lambda n: "/usr/bin/xclip"), \
                mock.patch.object(clipboard, "_run", fake_run):
            self.assertEqual(clipboard.write_text("hello"), "xclip")
        self.assertEqual(calls[0][0], ("xclip", "-selection", "clipboard", "-i"))
        self.assertEqual(calls[0][1], b"hello")

    def test_failed_helper_still_spools(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp) / "clipboard.txt"
            env = {"DISPLAY": ":0"}
            with mock.patch.object(clipboard, "sys", linux()), \
                    mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(clipboard.shutil, "which",
                                      lambda n: "/usr/bin/xclip"), \
                    mock.patch.object(clipboard, "_run",
                                      lambda *a, **k: types.SimpleNamespace(
                                          returncode=1, stdout=b"")):
                self.assertEqual(clipboard.write_text("kept", spool),
                                 "file:%s" % spool)
            self.assertEqual(spool.read_text(encoding="utf-8"), "kept")


class ReadTests(unittest.TestCase):
    def test_xclip_output_is_decoded(self):
        env = {"DISPLAY": ":0"}
        with mock.patch.object(clipboard, "sys", linux()), \
                mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(clipboard.shutil, "which",
                                  lambda n: "/usr/bin/xclip"), \
                mock.patch.object(clipboard, "_run",
                                  lambda argv, **k: types.SimpleNamespace(
                                      returncode=0,
                                      stdout="中文".encode("utf-8"))):
            self.assertEqual(clipboard.read_text(), "中文")

    def test_headless_read_returns_none_even_if_spool_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp) / "clipboard.txt"
            spool.write_text("old", encoding="utf-8")
            with mock.patch.object(clipboard, "sys", linux()), \
                    mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.object(clipboard.shutil, "which", lambda n: None):
                # Re-reading the spool would broadcast stale text on restart.
                self.assertIsNone(clipboard.read_text())


@unittest.skipUnless(sys.platform.startswith("win"), "needs user32")
class WindowsRoundTripTests(unittest.TestCase):
    """Regression: clip.exe re-encoded stdin via the console code page.

    A remote clipboard send of ``board-clip-测试-17599`` reached the Windows
    clipboard as ``'﻿??????'`` while the board logged the full text, i.e. the
    corruption happened only on the receiving side. Writing through user32
    keeps the payload UTF-16 end to end.
    """

    def setUp(self):
        self._saved = clipboard.read_text()

    def tearDown(self):
        if self._saved is not None:
            clipboard.write_text(self._saved)

    def test_chinese_survives_write(self):
        for text in ("board-clip-测试-17599", "中文剪贴板 ✓", "plain ascii"):
            self.assertEqual(clipboard.write_text(text), "win32")
            self.assertEqual(clipboard.read_text(), text)

    def test_written_text_has_no_bom(self):
        clipboard.write_text("剪贴板")
        self.assertEqual(clipboard.read_text(), "剪贴板")


class HealthTests(unittest.TestCase):
    def test_health_reports_clipboard_sink(self):
        p = health_payload({"alias": "b"}, 1.0, 2.0, 0, 8891,
                           clipboard="file")
        self.assertEqual(p["clipboard"], "file")

    def test_describe_prefers_gui_backend(self):
        with mock.patch.object(clipboard, "backend", lambda: "xclip"):
            self.assertEqual(clipboard.describe("/tmp/x"), "xclip")
        with mock.patch.object(clipboard, "backend", lambda: ""):
            self.assertEqual(clipboard.describe("/tmp/x"), "file")
            self.assertEqual(clipboard.describe(None), "none")


if __name__ == "__main__":
    unittest.main()
