"""Regression tests for authenticated, bounded file receiving."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from aiohttp import ClientSession, web

from flixcli import server
from flixcli.config import Store
from flixcli.model import BUBBLE_FILE
from flixcli.server import make_app


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
                        # Hardening: an unpaired sender cannot even register a
                        # file bubble now (it used to be stored for later).
                        r = await client.post("http://127.0.0.1:19591/bubble", json=bubble)
                        self.assertEqual(r.status, 403)
                        r = await client.post("http://127.0.0.1:19591/file",
                                              params={"share_id": rid, "file_name": "evil.bin"},
                                              data=b"evil")
                        self.assertNotEqual(r.status, 200)
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
                        # 2026-11-24 联调结论：这里回 409 会让官方端整单
                        # upload-failed（手机只显示"发送失败"），而"再发一次同名
                        # 图片"是完全正常的操作，所以改为改名落盘。
                        self.assertEqual(r.status, 200)
                        self.assertEqual(target.read_bytes(), b"original")
                        landed = downloads / "safe (1).bin"
                        self.assertTrue(landed.exists(), "collision did not land beside the original")
                        self.assertEqual(landed.read_bytes(), b"new!")
                        # 但同一个 share_id 重复上传仍然要拒绝（已提交）。
                        r = await client.post("http://127.0.0.1:19592/file",
                                              params={"share_id": rid, "file_name": "safe.bin"}, data=b"new!")
                        self.assertEqual(r.status, 409)
                finally:
                    await runner.cleanup(); store.close()
        asyncio.run(run())

    def test_unique_destination_numbering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "photo.jpg"
            first.write_bytes(b"a")
            self.assertEqual(server._unique_destination(str(first)),
                             str(root / "photo (1).jpg"))
            (root / "photo (1).jpg").write_bytes(b"b")
            self.assertEqual(server._unique_destination(str(first)),
                             str(root / "photo (2).jpg"))
            # 没有扩展名 / 多个点都要保持原样拼接
            dotless = root / "README"
            dotless.write_bytes(b"c")
            self.assertEqual(server._unique_destination(str(dotless)),
                             str(root / "README (1)"))
            multi = root / "archive.tar.gz"
            multi.write_bytes(b"d")
            self.assertEqual(server._unique_destination(str(multi)),
                             str(root / "archive.tar (1).gz"))


if __name__ == "__main__":
    unittest.main()
