"""Regression tests for authenticated, bounded file receiving."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from aiohttp import ClientSession, web

from flixcli.config import Store
from flixcli.model import BUBBLE_FILE
from flixcli.server import _sanitize_file_name, make_app


class FileNameSanitizeTests(unittest.TestCase):
    """Peer filenames are POSIX-legal but may be illegal on the receiving OS."""

    def test_windows_rewrites_illegal_chars_and_device_names(self):
        cases = {
            "linux:colon?.txt": "linux_colon_.txt",
            "a*b|c.txt": "a_b_c.txt",
            'quote"and<angle>.txt': "quote_and_angle_.txt",
            "trailing.": "trailing",
            "trailing . ": "trailing",
            "CON": "_CON",
            "nul.txt": "_nul.txt",
            "COM7.log": "_COM7.log",
            "tab\there.txt": "tab_here.txt",
            "": "file",
            "  ": "file",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(_sanitize_file_name(raw, windows=True), want)

    def test_posix_keeps_legal_names_untouched(self):
        for name in ("linux:colon?.txt", "a*b|c.txt", "trailing.", "CON"):
            with self.subTest(name=name):
                self.assertEqual(_sanitize_file_name(name, windows=False), name)

    def test_path_components_are_flattened_either_way(self):
        self.assertEqual(_sanitize_file_name("../../etc/passwd"), "passwd")
        self.assertEqual(_sanitize_file_name(r"C:\tmp\evil.txt", windows=True), "evil.txt")
        self.assertEqual(_sanitize_file_name(r"..\evil.txt", windows=False), "evil.txt")


class TransferSecurityTests(unittest.TestCase):
    def test_unpaired_and_source_mismatch_cannot_upload(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = Store(root / "db.sqlite")
                store.set_pref("saveDir", str(root / "downloads"))
                ident = {"fingerprint": "self", "alias": "test", "deviceModel": "Windows"}
                app = make_app(store, ident, lambda *_: None)
                runner = web.AppRunner(app); await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", 19591).start()
                try:
                    rid = str(uuid.uuid4())
                    bubble = {"id": rid, "from": "attacker", "to": "self", "type": BUBBLE_FILE,
                              "time": 1, "groupId": "", "content": {"state": 2,
                              "meta": {"resourceId": rid, "name": "evil.bin", "size": 4,
                                       "mimeType": "application/octet-stream"},
                              "waitingForAccept": True}}
                    async with ClientSession() as client:
                        r = await client.post("http://127.0.0.1:19591/bubble", json=bubble)
                        self.assertEqual(r.status, 200)
                        r = await client.post("http://127.0.0.1:19591/file",
                                              params={"share_id": rid, "file_name": "evil.bin"},
                                              data=b"evil")
                        self.assertEqual(r.status, 403)
                    self.assertFalse((root / "downloads" / "evil.bin").exists())
                finally:
                    await runner.cleanup(); store.close()
        asyncio.run(run())

    def test_paired_transfer_requires_exact_size_and_never_overwrites(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); downloads = root / "downloads"; downloads.mkdir()
                store = Store(root / "db.sqlite")
                store.set_pref("saveDir", str(downloads)); store.set_pref("max_receive_bytes", 32)
                sender = "sender"; ident = {"fingerprint": "self", "alias": "test", "deviceModel": "Windows"}
                store.upsert_device(sender, ip="127.0.0.1", port=19592); store.add_pair(sender, "0000")
                app = make_app(store, ident, lambda *_: None)
                runner = web.AppRunner(app); await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", 19592).start()
                try:
                    for rid, declared, body in ((str(uuid.uuid4()), 4, b"12345"),
                                                 (str(uuid.uuid4()), 4, b"123")):
                        bubble = {"id": rid, "from": sender, "to": "self", "type": BUBBLE_FILE,
                                  "time": 1, "groupId": "", "content": {"state": 2,
                                  "meta": {"resourceId": rid, "name": "safe.bin", "size": declared,
                                           "mimeType": "application/octet-stream"},
                                  "waitingForAccept": True}}
                        async with ClientSession() as client:
                            r = await client.post("http://127.0.0.1:19592/bubble", json=bubble)
                            self.assertEqual(r.status, 200)
                            r = await client.post("http://127.0.0.1:19592/file",
                                                  params={"share_id": rid, "file_name": "safe.bin"}, data=body)
                            self.assertNotEqual(r.status, 200)
                    target = downloads / "safe.bin"; target.write_bytes(b"original")
                    rid = str(uuid.uuid4())
                    bubble = {"id": rid, "from": sender, "to": "self", "type": BUBBLE_FILE,
                              "time": 1, "groupId": "", "content": {"state": 2,
                              "meta": {"resourceId": rid, "name": "safe.bin", "size": 4,
                                       "mimeType": "application/octet-stream"},
                              "waitingForAccept": True}}
                    async with ClientSession() as client:
                        await client.post("http://127.0.0.1:19592/bubble", json=bubble)
                        r = await client.post("http://127.0.0.1:19592/file",
                                              params={"share_id": rid, "file_name": "safe.bin"}, data=b"new!")
                        # A resend must succeed, but never clobber the old file.
                        self.assertEqual(r.status, 200)
                    self.assertEqual(target.read_bytes(), b"original")
                    self.assertEqual((downloads / "safe (1).bin").read_bytes(), b"new!")
                    # And a third copy keeps counting instead of reusing the name.
                    rid = str(uuid.uuid4())
                    bubble["id"] = rid
                    bubble["content"]["meta"]["resourceId"] = rid
                    async with ClientSession() as client:
                        await client.post("http://127.0.0.1:19592/bubble", json=bubble)
                        r = await client.post("http://127.0.0.1:19592/file",
                                              params={"share_id": rid, "file_name": "safe.bin"}, data=b"3rd!")
                        self.assertEqual(r.status, 200)
                    self.assertEqual((downloads / "safe (2).bin").read_bytes(), b"3rd!")
                finally:
                    await runner.cleanup(); store.close()
        asyncio.run(run())

    def test_hostile_peer_name_lands_under_sanitized_name(self):
        """A Linux peer's `linux:colon?.txt` must not fail after the transfer.

        Reproduces the board→PC case: NTFS treats ":" as an alternate data
        stream, so the O_EXCL reservation succeeded on "~/Downloads/linux"
        while os.replace onto the real name died with WinError 123 — the
        receiver answered 400 "upload-failed" with the payload fully received
        and left a stray empty file behind.
        """
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); downloads = root / "downloads"; downloads.mkdir()
                store = Store(root / "db.sqlite")
                store.set_pref("saveDir", str(downloads))
                sender = "sender"
                ident = {"fingerprint": "self", "alias": "test", "deviceModel": "Windows"}
                store.upsert_device(sender, ip="127.0.0.1", port=19593)
                store.add_pair(sender, "0000")
                app = make_app(store, ident, lambda *_: None)
                runner = web.AppRunner(app); await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", 19593).start()
                try:
                    raw = "linux:colon?.txt"
                    body = b"from a linux board"
                    rid = str(uuid.uuid4())
                    bubble = {"id": rid, "from": sender, "to": "self", "type": BUBBLE_FILE,
                              "time": 1, "groupId": "", "content": {"state": 2,
                              "meta": {"resourceId": rid, "name": raw, "size": len(body),
                                       "mimeType": "text/plain"},
                              "waitingForAccept": True}}
                    async with ClientSession() as client:
                        r = await client.post("http://127.0.0.1:19593/bubble", json=bubble)
                        self.assertEqual(r.status, 200)
                        r = await client.post("http://127.0.0.1:19593/file",
                                              params={"share_id": rid, "file_name": raw},
                                              data=body)
                        self.assertEqual(r.status, 200, await r.text())
                    saved = downloads / _sanitize_file_name(raw)
                    self.assertEqual(saved.read_bytes(), body)
                    # Nothing may be created under the raw (illegal) name, and
                    # in particular no NTFS data-stream stub ("linux").
                    self.assertFalse((downloads / "linux").exists())
                finally:
                    await runner.cleanup(); store.close()
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
