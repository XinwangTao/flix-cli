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


class ModelStatusSeverityTests(unittest.TestCase):
    """面板的严重度口径必须和 `flix models doctor` 一致。

    现场：只有 4 条**对端中继**不通时，面板原来写「不通路由 4 条」，
    看上去像本机坏了；而 doctor 已经把它降级成提示。两边说法不一致会误导排查。
    """

    @staticmethod
    def _render(live: dict) -> str:
        """把面板那段拼接逻辑跑一遍（不建窗口，避免依赖显示环境）。"""
        upstream = "上游正常"
        if live.get("models"):
            upstream += f"（{live['models']} 个模型）"
        routes = live.get("routes") or {}
        dead_direct = [model_id for model_id, row in routes.items()
                       if not row.get("ok", True)
                       and model_id.count("--") <= 1]
        dead_relay = [model_id for model_id, row in routes.items()
                      if not row.get("ok", True)
                      and model_id.count("--") > 1]
        if dead_direct:
            upstream += f"｜直连路由不通 {len(dead_direct)} 条"
        prefixes = live.get("relay_prefixes") or {}
        if prefixes:
            upstream += (f"｜对端中继 {len(prefixes)} 个前缀暂不可用"
                         "（相关模型已隐藏）")
        elif dead_relay:
            upstream += f"｜对端中继路由不通 {len(dead_relay)} 条"
        return upstream

    def test_dead_relays_are_described_as_peer_issue(self):
        live = {
            "models": 11,
            "routes": {
                "ccs-claude-a--ccs-claude-x--glm-5.3": {"ok": False},
                "ccs-claude-b--ccs-claude-x--glm-5.3": {"ok": False},
            },
            "relay_prefixes": {"ccs-claude-a--": 0, "ccs-claude-b--": 0},
        }
        text = self._render(live)
        self.assertIn("对端中继 2 个前缀暂不可用", text)
        self.assertIn("相关模型已隐藏", text)
        self.assertNotIn("不通路由", text,
                         "把对端中继说成「不通路由」会让人以为本机坏了")

    def test_dead_direct_route_is_called_out(self):
        live = {
            "models": 3,
            "routes": {"ccs-claude-openai--glm-5.3": {"ok": False}},
            "relay_prefixes": {},
        }
        text = self._render(live)
        self.assertIn("直连路由不通 1 条", text)

    def test_healthy_gateway_says_nothing_alarming(self):
        text = self._render({"models": 11, "routes": {}, "relay_prefixes": {}})
        self.assertNotIn("不通", text)
        self.assertIn("11 个模型", text)


class TokenMaskTests(unittest.TestCase):
    """面板不能明文显示网关令牌。

    这个软件本身就是拿来共享屏幕的，面板一旦出现在共享画面里，等于把凭据
    直接给了看的人。手机端 App 早就做了首 6 位 + 末 4 位的掩码，两端口径一致。
    """

    def test_long_token_keeps_only_head_and_tail(self):
        masked = gui._mask_token("rd-abcdefghijklmnopqrstuvwxyz")
        self.assertEqual(masked, "rd-abc…wxyz")
        self.assertNotIn("defghijklmnopqrstuvw", masked)

    def test_short_or_empty_token_is_fully_hidden(self):
        self.assertEqual(gui._mask_token("rd-short"), "…")
        self.assertEqual(gui._mask_token(""), "")
        self.assertEqual(gui._mask_token(None), "")

    def test_status_line_no_longer_contains_the_raw_token(self):
        import inspect

        source = inspect.getsource(gui.MainWindow._refresh_model_status)
        self.assertIn("_mask_token", source,
                      "面板状态行必须经过掩码")
        self.assertNotIn('令牌 {token}', source,
                         "仍在用未掩码的 token 变量拼状态行")


class PanelStateTests(unittest.TestCase):
    """面板的状态措辞：必须先看「用户开没开」，再看「端口有没有人听」。

    实测：全新安装（共享关闭）时面板写着「运行中」，因为 port_open() 只证明那个
    端口有进程在听——而它可能是**另一个实例**。用户会以为自己的共享已经开着。
    """

    def test_state_prefers_enabled_over_port_busy(self):
        import inspect

        source = inspect.getsource(gui.MainWindow._refresh_model_status)
        # enabled 的判断必须出现在 port_open 之前
        enabled_at = source.index("enabled = bool")
        port_at = source.index("port_busy = bool")
        self.assertLess(enabled_at, port_at,
                        "先看 enabled 再看端口，否则「未开启」会被写成「运行中」")
        self.assertIn("未开启", source)
        self.assertIn("端口被别的实例占用", source)

    def test_running_requires_both_enabled_and_listening(self):
        import inspect

        source = inspect.getsource(gui.MainWindow._refresh_model_status)
        # 「运行中」只能在 enabled 且端口在听的分支里出现
        self.assertIn("elif port_busy:", source)
        self.assertNotIn("if running:\n            state = \"运行中\"", source)


class CascadeToggleTests(unittest.TestCase):
    """级联（上游指向局域网内另一台 RelayDesk）必须能在 GUI 里配。

    之前 `save_model_upstream` 没传 allow_remote_upstream，用户填局域网地址只
    得到「请用 CLI: --allow-remote-upstream」的报错 —— 核心功能在面板里配不了。
    测试一律用临时 store，绝不能碰真实 prefs（写错了会把生产上游改坏）。
    """

    def test_checked_box_lets_save_accept_a_lan_upstream(self):
        from flixcli.model_gateway import load_config, save_config

        with tempfile.TemporaryDirectory() as tmp:
            store = config.Store(Path(tmp) / "db.sqlite")
            try:
                # 未开启时，局域网地址必须被拒
                with self.assertRaises(ValueError):
                    save_config(store, upstream="http://192.168.1.10:18993")
                # 勾选后（等价于面板传 allow_remote_upstream=True）才行
                cfg = save_config(store, upstream="http://192.168.1.10:18993",
                                  allow_remote_upstream=True)
                self.assertEqual(cfg["upstream"], "http://192.168.1.10:18993")
                self.assertTrue(cfg["allow_remote_upstream"])
                self.assertEqual(load_config(store)["upstream"],
                                 "http://192.168.1.10:18993")
            finally:
                store.close()

    def test_loopback_keeps_working_without_the_toggle(self):
        from flixcli.model_gateway import save_config

        with tempfile.TemporaryDirectory() as tmp:
            store = config.Store(Path(tmp) / "db.sqlite")
            try:
                cfg = save_config(store, upstream="http://127.0.0.1:15721",
                                  allow_remote_upstream=False)
                self.assertEqual(cfg["upstream"], "http://127.0.0.1:15721")
                self.assertFalse(cfg["allow_remote_upstream"])
            finally:
                store.close()

    def test_panel_wires_the_toggle_into_save(self):
        """面板必须把勾选状态传给 save_config，否则又回到「请用 CLI」。

        这里只做源码级断言：构造完整 MainWindow 会让 Qt 在 pytest 里析构线程并
        直接 abort（实测 0xC0000409）。配置行为已由上面两个用例覆盖。
        """
        import inspect

        source = inspect.getsource(gui.MainWindow.save_model_upstream)
        self.assertIn("allow_remote_upstream", source)
        self.assertIn("model_allow_remote", source)
        # 面板上确实有这个控件，且连到保存逻辑
        build = inspect.getsource(gui.MainWindow.__init__)
        self.assertIn("model_allow_remote", build + source)


if __name__ == "__main__":
    unittest.main()
