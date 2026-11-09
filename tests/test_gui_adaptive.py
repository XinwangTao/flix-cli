"""Adaptive layout, material gating and motion preference.

Three things the upstream sheet cannot express on its own: the sidebar has to
follow the window width, labels must ellipsize at whatever width they really
get instead of a build-time guess, and Windows accessibility switches must be
able to suspend the translucent material.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import PySide6  # noqa: F401
except ImportError:  # 基础环境没有 GUI 依赖时跳过
    raise unittest.SkipTest("PySide6 not installed")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from flixcli import gui

APP = QApplication.instance() or QApplication([])

gui.LocalApi.discover = staticmethod(lambda port=0: None)
gui.MainWindow.start_local_daemon = lambda self: None
gui.MainWindow._discovery_done = lambda self, found: None


def snapshot():
    image = QImage(1600, 1000, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.darkGreen)
    return image, QPoint(0, 0)


class AdaptiveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(
            gui, "PREFS_FILE", Path(self._tmp.name) / "prefs.json")
        self._patch.start()
        self.window = gui.MainWindow(19998, snapshot=snapshot())
        self.window.timer.stop()
        self.window.offline_mode = True
        self.window.show()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        APP.processEvents()
        self._patch.stop()
        self._tmp.cleanup()

    def test_sidebar_narrows_at_the_breakpoint(self):
        self.window.resize(1100, 760)
        APP.processEvents()
        self.assertEqual(self.window.sidebar_panel.width(), gui.MainWindow.SIDEBAR_NARROW)
        self.window.resize(1280, 840)
        APP.processEvents()
        self.assertEqual(self.window.sidebar_panel.width(), gui.MainWindow.SIDEBAR_WIDE)

    def test_elided_label_follows_its_real_width(self):
        label = gui.ElidedLabel("珠海飞达电子有限公司二号厂房装配线")
        label.show()
        APP.processEvents()
        label.resize(72, 20)
        APP.processEvents()
        self.assertTrue(label.text().endswith("…"), label.text())
        label.resize(480, 20)
        APP.processEvents()
        self.assertEqual(label.text(), label.fullText())

    def test_high_contrast_suspends_glass(self):
        self.window.set_glass(True)
        self.assertTrue(self.window._glass_visible())
        with mock.patch.object(gui, "system_contrast_enabled", return_value=True):
            self.assertFalse(self.window._glass_visible())
            self.window._restyle()
            self.assertFalse(self.window.glass_check.isChecked())
            self.assertFalse(self.window.glass_check.isEnabled())
            self.assertTrue(self.window.glass, "the preference itself is kept")
        self.window._restyle()
        self.assertTrue(self.window._glass_visible())

    def test_motion_preference_skips_the_page_fade(self):
        self.window.set_animations(False)
        self.window.set_mode("settings")
        APP.processEvents()
        self.assertEqual(self.window.workspace_stack.currentIndex(), 4)
        self.assertIsNone(self.window.workspace_stack.currentWidget().graphicsEffect())
        self.assertFalse(self.window.motion_check.isChecked())


class GlassSheetTests(unittest.TestCase):
    def test_glass_only_touches_the_functional_layer(self):
        clear = gui.styles_for("dark")
        glass = gui.styles_for("dark", glass=True)
        self.assertIn("QWidget#glassRoot { background: transparent; }", clear)
        self.assertIn("QFrame#rail { background: rgba(", glass)
        self.assertIn("QFrame#sidebar { background: rgba(", glass)
        self.assertIn("QStatusBar { background: rgba(", glass)
        self.assertNotIn("QWidget#page { background: rgba(", glass)


if __name__ == "__main__":
    unittest.main()
