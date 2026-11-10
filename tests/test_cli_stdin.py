"""Stdin payloads are UTF-8, not the Windows console code page.

`flix clipboard <ip> -` used to read `sys.stdin.read()`: on a Simplified-Chinese
Windows, redirected stdin is decoded with cp936, so a UTF-8 payload (the normal
output of git/ssh/editors/agents) became lone surrogates and the command died
in json.dumps with
    UnicodeEncodeError: 'utf-8' codec can't encode character '\udcad'
— the peer received nothing and no clipboard ever synced. `send-text` did not
even accept "-", so a piped message was sent as the literal string "-".
"""
from __future__ import annotations

import asyncio
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flixcli import cli
from flixcli.config import Store


class StdinTextTests(unittest.TestCase):
    def _stdin(self, raw: bytes, encoding: str, errors: str = "surrogateescape"):
        return io.TextIOWrapper(io.BytesIO(raw), encoding=encoding, errors=errors)

    def test_utf8_bytes_are_not_decoded_with_the_locale_codec(self):
        text = "e2e-剪贴板-pc2board ✓"
        wrapper = self._stdin(text.encode("utf-8"), "cp936")
        with mock.patch.object(sys, "stdin", wrapper):
            self.assertEqual(cli._read_stdin_text(), text)

    def test_utf8_bom_is_dropped(self):
        wrapper = self._stdin("﻿中文".encode("utf-8"), "cp936")
        with mock.patch.object(sys, "stdin", wrapper):
            self.assertEqual(cli._read_stdin_text(), "中文")

    def test_powershell_utf16le_is_decoded(self):
        wrapper = self._stdin("﻿中文\r\n".encode("utf-16-le"), "cp936")
        with mock.patch.object(sys, "stdin", wrapper):
            self.assertEqual(cli._read_stdin_text(), "中文\n")

    def test_crlf_folds_to_lf_like_text_mode(self):
        wrapper = self._stdin(b"a\r\nb\rc\n", "cp936")
        with mock.patch.object(sys, "stdin", wrapper):
            self.assertEqual(cli._read_stdin_text(), "a\nb\nc\n")

    def test_legacy_non_utf8_stdin_still_produces_text(self):
        # GBK bytes for 中文: not valid UTF-8, must fall back instead of raising.
        wrapper = self._stdin("中文".encode("gbk"), "cp936")
        with mock.patch.object(sys, "stdin", wrapper):
            self.assertIsInstance(cli._read_stdin_text(), str)


class PipedCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _args(self, **kw):
        args = mock.Mock(to="peer", text="-", port=8891, json=False)
        for k, v in kw.items():
            setattr(args, k, v)
        return args

    def test_send_text_dash_reads_utf8_stdin(self):
        sent = {}

        async def fake_send(store, ident, to, text, port=None):
            sent["text"] = text
            return {"ok": True, "to": to}

        wrapper = io.TextIOWrapper(
            io.BytesIO("中文 ✓\n".encode("utf-8")), encoding="cp936",
            errors="surrogateescape")
        with mock.patch.object(sys, "stdin", wrapper), \
                mock.patch.object(cli, "Store", lambda: self.store), \
                mock.patch.object(cli, "load_or_create_identity",
                                  lambda s: {"fingerprint": "me"}), \
                mock.patch("flixcli.outbox.send_text", fake_send):
            code = asyncio.run(cli.cmd_send_text(self._args()))
        self.assertEqual(code, 0)
        self.assertEqual(sent["text"], "中文 ✓\n")

    def test_clipboard_dash_reads_utf8_stdin(self):
        sent = {}

        async def fake_send(store, ident, to, kind, text, port_arg):
            sent["kind"], sent["text"] = kind, text
            return 0

        wrapper = io.TextIOWrapper(
            io.BytesIO("剪贴板 ✓".encode("utf-8")), encoding="cp936",
            errors="surrogateescape")
        with mock.patch.object(sys, "stdin", wrapper), \
                mock.patch.object(cli, "Store", lambda: self.store), \
                mock.patch.object(cli, "load_or_create_identity",
                                  lambda s: {"fingerprint": "me"}), \
                mock.patch.object(cli, "_send", fake_send):
            code = asyncio.run(cli.cmd_clipboard(self._args()))
        self.assertEqual(code, 0)
        self.assertEqual(sent["kind"], "clipboard")
        self.assertEqual(sent["text"], "剪贴板 ✓")


if __name__ == "__main__":
    unittest.main()
