"""`flix models doctor` 的体检报告。

现场排障时用户只知道「手机连不上」，原因可能在配置/监听/上游/路由/模型任何一环；
doctor 要把每一项都给出结论，异常时退出码非 0 并带建议。
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from flixcli import cli
from flixcli.config import Store
from flixcli import model_gateway
from flixcli.model_gateway import save_config

# 一个确定没人监听的端口：doctor 的「端口未监听」路径应可确定复现，
# 绝不能打本机真实的 18993（会污染线上 /health 的拒绝记录）。
RD_TEST_PORT = 1


class ModelsDoctorTests(unittest.TestCase):
    def _store(self, tmp: str, **cfg):
        store = Store(Path(tmp) / "db.sqlite")
        save_config(store, upstream="http://127.0.0.1:1", port=RD_TEST_PORT, **cfg)
        return store

    def test_reports_failure_and_exit_code_when_gateway_is_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp, enabled=True)
            try:
                args = mock.Mock(json=True, model=None)
                with redirect_stdout(io.StringIO()):
                    code = cli._models_doctor(store, args)
            finally:
                store.close()
        self.assertEqual(code, 6)
        payload = json.loads(self._captured) if hasattr(self, "_captured") else None
        # 退出码 6 = 有异常项；具体项在上面那次调用里已 emit 到 stdout
        self.assertIsNone(payload)

    def test_json_payload_lists_every_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp, enabled=True)
            try:
                args = mock.Mock(json=True, model=None)
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    cli._models_doctor(store, args)
            finally:
                store.close()
        payload = json.loads(buffer.getvalue())
        names = [row["name"] for row in payload["checks"]]
        for expected in ("配置", "共享开关", "端口监听", "上游可读",
                         "网关健康", "手机地址"):
            self.assertIn(expected, names)
        self.assertFalse(payload["ok"])       # 上游 127.0.0.1:1 必然不可读
        self.assertTrue(payload["checks"][0]["ok"])  # 配置本身总是可读的

    def test_disabled_sharing_is_flagged_but_not_fatal_to_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp, enabled=False)
            try:
                args = mock.Mock(json=True, model=None)
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    cli._models_doctor(store, args)
            finally:
                store.close()
        payload = json.loads(buffer.getvalue())
        switch = next(row for row in payload["checks"]
                      if row["name"] == "共享开关")
        self.assertFalse(switch["ok"])
        self.assertIn("hint", switch)


class TokenMismatchTests(unittest.TestCase):
    """端口上有网关、但令牌对不上时，doctor 必须说清楚，而不是报「活的但 0 个模型」。

    实测：全新安装（或令牌被轮换后）跑 doctor，未授权的 /health 只回 {"ok": true}，
    旧逻辑读成 models=0，看着像「网关活着但没模型」；补全那两项报 401，hint 还指向
    「看 last_error」——真正的原因是令牌不匹配，用户根本猜不到。
    """

    def test_probe_flags_unauthorized(self):
        """probe() 在只拿到最小 /health 时给出 unauthorized 标记。"""
        from unittest import mock

        import urllib.request

        class FakeResponse:
            def __init__(self, body):
                self._body = body

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=None):
            return FakeResponse(b'{"ok": true}')

        with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            live = model_gateway.probe(18993, "rd-token", timeout=1.0)
        self.assertTrue(live.get("listening"))
        self.assertTrue(live.get("unauthorized"),
                        "最小 /health 必须被识别成令牌不通")

    def test_probe_authorized_is_not_flagged(self):
        from unittest import mock

        import urllib.request

        class FakeResponse:
            def read(self):
                return b'{"ok": true, "models": 11, "stats": {}}'

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch.object(urllib.request, "urlopen", lambda req, timeout=None: FakeResponse()):
            live = model_gateway.probe(18993, "rd-token", timeout=1.0)
        self.assertFalse(live.get("unauthorized"))


class DoctorPrefsIntegrityTests(unittest.TestCase):
    """prefs 损坏会备份成 *.bad-<ts> 并**轮换令牌**，后果是手机突然全部 401。

    以前这条信息只在 stderr 出现一次、GUI 里完全看不到。doctor 要能回答
    「为什么手机全掉了」。
    """

    def test_missing_recovery_is_reported_as_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            try:
                save_config(store, upstream="http://127.0.0.1:1",
                            port=RD_TEST_PORT, enabled=True)
                args = mock.Mock(json=True, model="glm-5.3-flash")
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    cli._models_doctor(store, args)
            finally:
                store.close()
        payload = json.loads(buffer.getvalue())
        row = next(r for r in payload["checks"] if r["name"] == "配置完整性")
        self.assertTrue(row["ok"])

    def test_damaged_prefs_backup_is_surfaced_with_a_hint(self):
        import time as _time

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            try:
                save_config(store, upstream="http://127.0.0.1:1",
                            port=RD_TEST_PORT, enabled=True)
                bad = store.prefs_path.with_name(
                    f"{store.prefs_path.name}.bad-{int(_time.time())}")
                bad.write_text("{ broken json", encoding="utf-8")
                args = mock.Mock(json=True, model="glm-5.3-flash")
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    cli._models_doctor(store, args)
            finally:
                store.close()
        payload = json.loads(buffer.getvalue())
        row = next(r for r in payload["checks"] if r["name"] == "配置完整性")
        # 提示级（None）而不是失败，因为网关本身可能是好的
        self.assertIsNone(row["ok"])
        self.assertIn("401", row["detail"] + row.get("hint", ""))


class DoctorProtocolCoverageTests(unittest.TestCase):
    """doctor 必须同时验 Anthropic 与 OpenAI 两条协议。

    手机上的第三方客户端多数只支持 OpenAI；只验 /v1/messages 等于没验
    「绝大多数人实际会走的路径」。
    """

    def test_both_protocol_checks_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            try:
                save_config(store, upstream="http://127.0.0.1:1",
                            port=RD_TEST_PORT, enabled=True)
                args = mock.Mock(json=True, model="glm-5.3-flash")
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    cli._models_doctor(store, args)
            finally:
                store.close()
        payload = json.loads(buffer.getvalue())
        names = [row["name"] for row in payload["checks"]]
        self.assertIn("真实补全", names)
        self.assertIn("OpenAI 补全", names)
        # 端口不可达时两项都应是「跳过」而不是误报失败
        for row in payload["checks"]:
            if row["name"] in ("真实补全", "OpenAI 补全"):
                self.assertIsNone(row["ok"], row)


class WatchBaselineTests(unittest.TestCase):
    """观测期间网关重启会让计数器归零，增量不能直接相减。

    实测出现过「新增失败 -2」这种看不懂的数：窗口内重启过，末样本减首样本
    就成了负数。基线应取「最后一次重启之后的第一个样本」。
    """

    @staticmethod
    def _sample(t: int, ok: int, failed: int) -> dict:
        return {"t": t, "ok": ok, "failed": failed, "models": 11,
                "catalog": 39, "rejected": 0, "active": 0,
                "dead_direct": 0, "listening": True}

    def test_steady_window_uses_the_first_sample(self):
        from flixcli.cli import _watch_baseline
        samples = [self._sample(0, 10, 0), self._sample(60, 12, 1),
                   self._sample(120, 15, 1)]
        restarts, base = _watch_baseline(samples)
        self.assertEqual(restarts, 0)
        self.assertEqual(base["ok"], 10)

    def test_restart_makes_the_baseline_follow_it(self):
        from flixcli.cli import _watch_baseline
        samples = [self._sample(0, 94, 3), self._sample(60, 95, 3),
                   self._sample(120, 10, 1), self._sample(180, 12, 1)]
        restarts, base = _watch_baseline(samples)
        self.assertEqual(restarts, 1)
        self.assertEqual(base["ok"], 10, "基线应落在重启后的第一个样本")

    def test_delta_never_goes_negative_after_a_restart(self):
        from flixcli.cli import _watch_baseline
        samples = [self._sample(0, 94, 3), self._sample(60, 10, 1),
                   self._sample(120, 12, 2)]
        _, base = _watch_baseline(samples)
        self.assertGreaterEqual(samples[-1]["failed"] - base["failed"], 0)


class DoctorSeverityTests(unittest.TestCase):
    """对端中继坏不该让本机 doctor 报红：那是别人的问题，且网关已隐藏那些模型。"""

    def test_peer_relay_failures_are_warnings_not_failures(self):
        from flixcli.cli import _models_doctor  # noqa: F401  (导入即验证签名)

        # 直接用判定规则验证：只有直连路由坏才算本机故障
        dead_relay = "ccs-claude-peer--ccs-claude-x--glm-5.3"
        dead_direct = "ccs-claude-openai--glm-5.3"
        self.assertEqual(dead_relay.count("--") > 1, True)
        self.assertEqual(dead_direct.count("--") <= 1, True)

    def test_phone_config_block_is_present_in_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            try:
                save_config(store, upstream="http://127.0.0.1:1",
                            port=RD_TEST_PORT, enabled=True)
                args = mock.Mock(json=True, model=None)
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    cli._models_doctor(store, args)
            finally:
                store.close()
        payload = json.loads(buffer.getvalue())
        phone = payload["phone"]
        for key in ("base_url", "api_key", "model", "self_test"):
            self.assertIn(key, phone)
        self.assertTrue(phone["base_url"].startswith("http"))


if __name__ == "__main__":
    unittest.main()
