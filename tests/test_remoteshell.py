"""Remote exec (rdShell): envelope shape, executor limits, trust gate, audit."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from flixcli import model, server
from flixcli.channel import _error_detail
from flixcli.config import Store
from flixcli.remoteshell import MAX_OUTPUT_BYTES, run_command


class ShellIntentTests(unittest.TestCase):
    def test_intent_shape(self):
        intent = model.shell_intent("fp-me", "fp-peer", "sid-1", "echo hi", 12)
        self.assertEqual(intent["action"], model.ACTION_SHELL)
        self.assertEqual(intent["deviceId"], "fp-me")
        self.assertEqual(intent["extra"], {"to": "fp-peer", "id": "sid-1",
                                           "command": "echo hi", "timeout": 12.0})

    def test_action_is_an_rd_extension(self):
        self.assertTrue(model.ACTION_SHELL.startswith("rd"))
        self.assertNotIn(model.ACTION_SHELL, {
            model.ACTION_CLIPBOARD, model.ACTION_ASK_PAIR,
            model.ACTION_CONFIRM_PAIR, model.ACTION_CONFIRM_RECEIVE})


class RunCommandTests(unittest.TestCase):
    def test_success_captures_stdout(self):
        result = run_command("echo hello", timeout=10)
        self.assertEqual(result["exit"], 0)
        self.assertIn("hello", result["stdout"])
        self.assertFalse(result["truncated"])
        self.assertFalse(result["timeout"])

    def test_nonzero_exit_is_reported_not_raised(self):
        self.assertEqual(run_command("exit 3", timeout=10)["exit"], 3)

    def test_timeout_kills_the_command(self):
        result = run_command(
            f'"{sys.executable}" -c "import time;time.sleep(10)"', timeout=1)
        self.assertTrue(result["timeout"])
        self.assertLess(result["durationMs"], 9000)

    def test_output_is_truncated(self):
        result = run_command(
            f'"{sys.executable}" -c "print(chr(120)*300000)"', timeout=20)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["stdout"]), MAX_OUTPUT_BYTES)

    def test_cjk_output_is_decoded_with_the_console_code_page(self):
        # cmd children write the OEM code page, not UTF-8; a strict UTF-8
        # decode would turn this into replacement characters.
        result = run_command("echo 中文测试", timeout=10)
        self.assertIn("中文测试", result["stdout"])


class ShellHttpTests(unittest.IsolatedAsyncioTestCase):
    """Drive the real /intent handler, as a peer would."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "relaydesk.db")
        self.store.add_pair("fp-peer", "1234")
        self.store.upsert_device("fp-peer", ip="127.0.0.1")
        app = server.make_app(self.store, {"fingerprint": "fp-me", "alias": "me"},
                              lambda name, payload: None)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self._tmp.cleanup()

    async def test_paired_peer_runs_and_is_audited(self):
        intent = model.shell_intent("fp-peer", "fp-me", "sid-1", "echo hi", 10)
        resp = await self.client.post("/intent", json=intent)
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["exit"], 0)
        self.assertIn("hi", body["stdout"])
        audit = self.store.shell_audit()
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["command"], "echo hi")
        self.assertEqual(audit[0]["exit"], 0)

    async def test_unpaired_sender_is_refused_and_not_audited(self):
        intent = model.shell_intent("fp-stranger", "fp-me", "sid-2",
                                    "echo nope", 10)
        resp = await self.client.post("/intent", json=intent)
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.store.shell_audit(), [])

    async def test_malformed_envelope_is_rejected(self):
        resp = await self.client.post(
            "/intent", json={"action": model.ACTION_SHELL,
                             "deviceId": "fp-peer", "extra": {"to": "fp-me"}})
        self.assertEqual(resp.status, 400)

    async def test_executor_failure_reports_the_reason(self):
        """A crash in the worker used to be a bare aiohttp 500 with no body.

        The peer could not tell it apart from a dead daemon, so it had nothing
        to show the operator. The handler must answer in the JSON shape the
        client already parses, and must not write an audit row for a command
        that never ran.
        """
        with mock.patch.object(server, "run_command",
                               side_effect=OSError("cannot spawn cmd.exe")):
            intent = model.shell_intent("fp-peer", "fp-me", "sid-boom", "whoami", 5)
            resp = await self.client.post("/intent", json=intent)
        self.assertEqual(resp.status, 500)
        body = await resp.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "shell-failed")
        self.assertEqual(body["id"], "sid-boom")
        self.assertIn("OSError", body["detail"])
        self.assertEqual(self.store.shell_audit(), [])

    async def test_audit_failure_does_not_fail_a_command_that_ran(self):
        with mock.patch.object(Store, "add_shell_audit",
                               side_effect=RuntimeError("database is locked")):
            intent = model.shell_intent("fp-peer", "fp-me", "sid-2", "echo hi", 10)
            resp = await self.client.post("/intent", json=intent)
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["exit"], 0)
        self.assertIn("hi", body["stdout"])


class ErrorDetailTests(unittest.TestCase):
    def test_json_handshake_payload_is_surfaced(self):
        detail = _error_detail(500, '{"ok": false, "error": "shell-failed", '
                                    '"detail": "OSError: nope"}')
        self.assertIn("shell-failed", detail)
        self.assertIn("OSError: nope", detail)

    def test_bare_aiohttp_500_gets_an_actionable_hint(self):
        detail = _error_detail(500, "500 Internal Server Error\n\n"
                                   "Server got itself in trouble")
        self.assertIn("peer crashed", detail)
        self.assertNotIn("Internal Server Error", detail)


if __name__ == "__main__":
    unittest.main()
