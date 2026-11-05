"""Settings-page update flow and in-conversation search scoping.

Two behaviours that only exist in the window: the updater is a CLI subcommand
behind a button, and the transcript search is per conversation.
"""
from __future__ import annotations

import os
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import PySide6  # noqa: F401
except ImportError:  # GUI 是可选依赖，基础环境（CI）里跳过
    raise unittest.SkipTest("PySide6 not installed")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import flixcli.gui as gui  # noqa: E402

APP = QApplication.instance() or QApplication([])

gui.LocalApi.discover = staticmethod(lambda port=0: None)
gui.MainWindow.start_local_daemon = lambda self: None
gui.MainWindow._discovery_done = lambda self, found: None

ME = "fp-me"
PEER = "fp-peer"
OTHER = "fp-other"


def overview() -> dict:
    return {
        "self": {"id": ME, "alias": "me", "port": 8898},
        "devices": [{"fingerprint": PEER, "alias": "TABLET", "os": "win",
                     "ip": "10.0.0.2", "port": 8899},
                    {"fingerprint": OTHER, "alias": "PHONE", "os": "win",
                     "ip": "10.0.0.3", "port": 8899}],
        "groups": [],
        "adb": {"devices": []},
        "media": {"sessions": []},
        "settings": {"saveDir": "/tmp"},
        "bubbles": [
            {"id": "a1", "from": PEER, "to": ME, "type": 0,
             "time": 1_700_000_000_000, "groupId": "", "content": "alpha from tablet"},
            {"id": "b1", "from": OTHER, "to": ME, "type": 0,
             "time": 1_700_000_060_000, "groupId": "", "content": "beta from phone"},
        ],
    }


def make_window() -> gui.MainWindow:
    window = gui.MainWindow(19999)
    window.timer.stop()
    window.offline_mode = True
    window._auto_start_tried = True
    window.resize(1000, 620)
    window.self_id = ME
    window.payload = overview()
    window.apply_overview(overview())
    window.entity_list.setCurrentRow(0)
    window.show()
    window.render_current()
    return window


def settle(window, ms: int = 200) -> None:
    deadline = time.monotonic() + ms / 1000
    while time.monotonic() < deadline:
        APP.processEvents()
        time.sleep(0.01)


def wait_worker(window) -> None:
    worker = window._update_worker
    if worker is not None:
        worker.wait(15000)
    settle(window)


class SearchScopeTests(unittest.TestCase):
    def setUp(self):
        self.window = make_window()

    def tearDown(self):
        self.window.close()

    def test_search_does_not_leak_into_the_next_conversation(self):
        self.window.session_search.setText("alpha")
        settle(self.window)
        self.assertEqual(self.window._timeline_ids, {"a1"})
        self.window.entity_list.setCurrentRow(1)  # switch to PHONE
        settle(self.window)
        self.assertEqual(self.window.session_search.text(), "")
        self.assertIn("b1", self.window._timeline_ids)

    def test_search_survives_a_plain_poll(self):
        self.window.session_search.setText("alpha")
        settle(self.window)
        self.window.apply_overview(overview())
        settle(self.window)
        self.assertEqual(self.window.session_search.text(), "alpha")


class UpdateFlowTests(unittest.TestCase):
    def setUp(self):
        self.window = make_window()
        self.window.set_mode("settings")

    def tearDown(self):
        self.window.close()

    def test_settings_page_shows_the_client_version(self):
        self.assertIn(gui.APP_VERSION, self.window.version_label.text())

    def test_same_version_says_up_to_date(self):
        info = {"ok": True, "status": "current", "current": "2.2.1",
                "latest": "v2.2.1"}
        with mock.patch.object(gui, "run_cli_json", return_value=info) as called:
            self.window.check_updates()
            wait_worker(self.window)
        self.assertEqual(called.call_args.args[0], ["update", "--check", "--json"])
        self.assertIn("已是最新", self.window.update_hint.text())
        self.assertFalse(self.window.update_now_button.isVisible())

    def test_new_version_offers_the_update_button(self):
        info = {"ok": True, "status": "update-available", "current": "2.2.1",
                "latest": "v2.3.0"}
        with mock.patch.object(gui, "run_cli_json", return_value=info), \
                mock.patch.object(self.window, "prefetch_update") as prefetch:
            self.window.check_updates()
            wait_worker(self.window)
        self.assertIn("v2.3.0", self.window.update_hint.text())
        self.assertTrue(self.window.update_now_button.isVisible())
        prefetch.assert_called_once()

    def test_new_version_shows_a_banner_and_starts_the_download(self):
        info = {"ok": True, "status": "update-available", "current": "2.2.1",
                "latest": "v2.3.0"}
        with mock.patch.object(gui, "run_cli_json", return_value=info), \
                mock.patch.object(self.window, "prefetch_update"):
            self.window.check_updates(silent=True)
            wait_worker(self.window)
        self.assertTrue(self.window.update_banner.isVisible())
        self.assertIn("正在后台下载", self.window.update_banner_text.text())
        self.assertFalse(self.window.update_banner_action.isVisible())

    def test_auto_check_is_silent_and_can_be_disabled(self):
        with mock.patch.object(self.window, "check_updates") as check:
            self.window.auto_check_updates()
        check.assert_called_once_with(silent=True)
        with mock.patch.object(self.window, "_load_prefs",
                               return_value={"auto_update": False}), \
                mock.patch.object(self.window, "check_updates") as check:
            self.window.auto_check_updates()
        check.assert_not_called()

    def test_silent_check_failure_stays_out_of_the_settings_page(self):
        with mock.patch.object(gui, "run_cli_json",
                               side_effect=RuntimeError("offline")):
            self.window.check_updates(silent=True)
            wait_worker(self.window)
        self.assertNotIn("检查失败", self.window.update_hint.text())
        self.assertTrue(self.window.check_update_button.isEnabled())

    def test_finished_download_offers_a_restart(self):
        self.window._update_info = {"latest": "v2.3.0"}
        info = {"ok": True, "status": "dry-run",
                "planned": {"wouldInstall": "v2.3.0"}}
        self.window.apply_update_ready(info)
        self.assertTrue(self.window.update_banner.isVisible())
        self.assertTrue(self.window.update_banner_action.isVisible())
        self.assertIn("已下载并校验", self.window.update_banner_text.text())
        self.assertIn("v2.3.0", self.window.update_hint.text())

    def test_banner_can_be_dismissed(self):
        self.window._show_update_banner("新版本已就绪", action=True)
        self.window.update_banner_dismiss.click()
        settle(self.window)
        self.assertFalse(self.window.update_banner.isVisible())

    def test_failed_check_does_not_enable_the_update(self):
        with mock.patch.object(gui, "run_cli_json",
                               side_effect=RuntimeError("network down")):
            self.window.check_updates()
            wait_worker(self.window)
        self.assertIn("检查失败", self.window.update_hint.text())
        self.assertFalse(self.window.update_now_button.isVisible())
        self.assertTrue(self.window.check_update_button.isEnabled())

    def test_declining_the_dialog_installs_nothing(self):
        self.window._update_info = {"latest": "v2.3.0"}
        with mock.patch.object(gui, "run_cli_json") as called, \
                mock.patch.object(gui.QMessageBox, "question",
                                  return_value=QMessageBox.No):
            self.window.install_update()
            settle(self.window)
        called.assert_not_called()

    def test_confirming_runs_the_install_and_reports_it(self):
        self.window._update_info = {"latest": "v2.3.0"}
        info = {"ok": True, "status": "installed", "installed": "v2.3.0"}
        with mock.patch.object(gui, "run_cli_json", return_value=info) as called, \
                mock.patch.object(gui.QMessageBox, "question",
                                  return_value=QMessageBox.Yes):
            self.window.install_update()
            wait_worker(self.window)
        self.assertEqual(called.call_args.args[0], ["update", "--json"])
        self.assertIn("v2.3.0", self.window.update_hint.text())


class CliCommandTests(unittest.TestCase):
    def test_source_checkout_goes_through_the_module(self):
        argv = gui.cli_command(["update", "--check"])
        self.assertEqual(argv[1:], ["-m", "flixcli.cli", "update", "--check"])

    def test_frozen_binary_re_enters_itself(self):
        with mock.patch.object(gui.sys, "frozen", True, create=True):
            argv = gui.cli_command(["update", "--check"])
        self.assertEqual(argv[1:], ["update", "--check"])
        self.assertEqual(argv[0], gui.sys.executable)


class CliJsonParsingTests(unittest.TestCase):
    def test_pretty_printed_payload_after_a_log_line(self):
        stdout = ('check exe=... local=2.2.1 latest=v2.2.1 -> current\n'
                  '{\n "ok": true,\n "status": "current",\n "current": "2.2.1"\n}\n')
        payload = gui.parse_cli_json(stdout)
        self.assertEqual(payload["status"], "current")

    def test_no_payload_returns_none(self):
        self.assertIsNone(gui.parse_cli_json("boom: no json here"))
        self.assertIsNone(gui.parse_cli_json("{\n \"broken\": \n"))

    def test_run_cli_json_surfaces_a_failed_verdict(self):
        proc = mock.Mock(returncode=4,
                         stdout='{\n "ok": false,\n "error": "network"\n}',
                         stderr="")
        with mock.patch("subprocess.run", return_value=proc):
            with self.assertRaises(RuntimeError) as caught:
                gui.run_cli_json(["update", "--check"])
        self.assertIn("network", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
