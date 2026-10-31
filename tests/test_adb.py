"""Focused tests for the safe ADB adapter."""
from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

from flixcli.adb import AdbClient, AdbDevice


class AdbAdapterTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "MSYS 路径还原只在 Windows 上生效")
    def test_msys_path_is_restored_only_for_android_roots(self):
        client = AdbClient()
        with patch.dict(os.environ, {
            "MSYSTEM": "MINGW64",
            "SHELL": r"D:\Program Files\Git\bin\bash.exe",
        }, clear=False):
            self.assertEqual(
                client._restore_msys_remote_path(
                    r"D:\Program Files\Git\sdcard\Download\x.txt"),
                "/sdcard/Download/x.txt")
            self.assertEqual(
                client._restore_msys_remote_path(
                    r"D:\Program Files\Git\usr\bin\x"),
                r"D:\Program Files\Git\usr\bin\x")

    def test_device_parser_marks_authorized_state(self):
        client = AdbClient()
        client._run = AsyncMock(return_value=(0, "List of devices attached\n"
            "cc75218b\tdevice product:elish model:M2105K81AC transport_id:5\n"
            "offline-1\toffline product:x model:Broken transport_id:6\n", ""))
        rows = asyncio.run(client.devices())
        self.assertEqual(rows[0].serial, "cc75218b")
        self.assertTrue(rows[0].as_dict()["authorized"])
        self.assertFalse(rows[1].as_dict()["authorized"])

    def test_shell_requires_approval_before_device_lookup(self):
        client = AdbClient()
        client.devices = AsyncMock(side_effect=AssertionError("must not run"))
        result = asyncio.run(client.shell(["echo", "blocked"], serial="cc75218b"))
        self.assertEqual(result["error"], "approval_required")
        client.devices.assert_not_awaited()

    def test_serial_selection_rejects_ambiguous_devices(self):
        client = AdbClient()
        devices = [AdbDevice("usb", "device"), AdbDevice("wifi", "device")]
        with self.assertRaisesRegex(Exception, "specify --serial"):
            client._require_serial(None, devices)
        self.assertEqual(client._require_serial("usb", devices), "usb")


if __name__ == "__main__":
    unittest.main()
