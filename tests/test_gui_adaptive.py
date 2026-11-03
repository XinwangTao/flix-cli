"""Adaptive layout: narrow sidebar, resize-time elision, high-contrast fallback."""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from flixcli import gui  # noqa: E402

APP = QApplication.instance() or QApplication([])


class AdaptiveUITest(unittest.TestCase):
    def setUp(self):
        self.window = gui.MainWindow(19999)  # nothing listening: refresh just fails
        self.window.animations = False
        self.window.show()

    def tearDown(self):
        self.window.timer.stop()
        self.window.close()
        self.window.deleteLater()
        APP.processEvents()

    def test_sidebar_narrows_with_the_window(self):
        self.window.resize(1100, 760)
        APP.processEvents()
        self.assertEqual(self.window.sidebar_panel.minimumWidth(),
                         gui.MainWindow.SIDEBAR_NARROW)
        self.window.resize(1280, 840)
        APP.processEvents()
        self.assertEqual(self.window.sidebar_panel.minimumWidth(),
                         gui.MainWindow.SIDEBAR_WIDE)

    def test_elided_label_refollows_its_width(self):
        full = "产线具体情况收集表-珠海-2026"
        label = gui.ElidedLabel(full)
        label.show()  # hidden widgets defer their resize event until shown
        label.resize(72, 20)
        APP.processEvents()
        self.assertTrue(label.text().endswith("…"))
        self.assertEqual(label.toolTip(), full)
        label.resize(480, 20)
        APP.processEvents()
        self.assertEqual(label.text(), full)

    def test_high_contrast_suspends_glass(self):
        self.window.glass = True
        original = gui.system_contrast_enabled
        gui.system_contrast_enabled = lambda: True
        try:
            self.window._restyle()
            self.assertFalse(self.window._glass_visible())
            self.assertFalse(self.window.glass_buttons[True].isEnabled())
            self.assertTrue(self.window.glass_buttons[False].isChecked())
        finally:
            gui.system_contrast_enabled = original
            self.window._restyle()


if __name__ == "__main__":
    unittest.main()
