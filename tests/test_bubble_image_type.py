"""Official Android Felix picture bubbles must land (interop, 2026-11-24).

Wire fact captured from the peer's logcat while the phone showed "等待对方确认":

    send primitiveBubble = PrimitiveFileBubble{id: e87cf8f5...}
    _sendBasicBubble body={"type":1,"content":{"state":2,...,"meta":{
        "name":"IMG_...png","mimeType":"image/png","size":1277976,
        "thumbnailBase64":"/9j/4AAQ..."}}}

Only type 3 was handled, so the receiver answered 200 without storing anything
and the sender waited forever for a confirmReceive that referenced a bubble the
receiver had thrown away. These tests pin the fix.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from flixcli import model, server
from flixcli.config import Store


def _image_bubble(bubble_id: str = "bid-img") -> dict:
    return {
        "id": bubble_id,
        "from": "fp-peer",
        "to": "fp-me",
        "type": model.BUBBLE_IMAGE,
        "time": 1795508342000,
        "groupId": "",
        "content": {
            "state": 2, "progress": 0.0, "speed": 0, "receiveBytes": 0,
            "waitingForAccept": True,
            "meta": {
                "name": "IMG_20261124_161901.png",
                "mimeType": "image/png",
                "nameWithSuffix": "IMG_20261124_161901.png",
                "androidContentUri": "",
                "size": 1277976,
                "width": 0, "height": 0,
                "thumbnailBase64": "/9j/4AAQSkZJRgABAQAAAQ",
            },
        },
    }


class PictureBubbleHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "relaydesk.db")
        self.store.add_pair("fp-peer", "1234")
        self.store.upsert_device("fp-peer", ip="127.0.0.1", port=8891)
        self.events: list[tuple[str, dict]] = []
        app = server.make_app(
            self.store, {"fingerprint": "fp-me", "alias": "me"},
            lambda name, payload: self.events.append((name, payload)))
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self._tmp.cleanup()

    async def test_picture_bubble_is_stored_and_confirmed(self):
        with mock.patch("flixcli.channel.post_intent",
                        new=mock.AsyncMock(return_value="{0: success}")) as posted:
            resp = await self.client.post("/bubble", json=_image_bubble())
            body = (await resp.text()).strip()
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, "received bubble")

        row = self.store.con.execute(
            "SELECT type FROM bubble_entities WHERE id='bid-img'").fetchone()
        self.assertIsNotNone(row, "picture bubble was acked but not stored")
        self.assertEqual(row[0], model.BUBBLE_FILE,
                         "picture bubbles are normalized to the file type")
        file_row = self.store.con.execute(
            "SELECT name, mime_type, size FROM file_contents WHERE id='bid-img'"
        ).fetchone()
        self.assertEqual(file_row[0], "IMG_20261124_161901.png")
        self.assertEqual(file_row[1], "image/png")
        self.assertEqual(file_row[2], 1277976)
        # The sender stays stuck unless it gets the confirmReceive ack.
        for _ in range(20):
            if posted.await_count:
                break
            await __import__("asyncio").sleep(0.01)
        self.assertEqual(posted.await_count, 1)
        intent = posted.await_args.args[1]
        self.assertEqual(intent["action"], model.ACTION_CONFIRM_RECEIVE)
        self.assertEqual(intent["bubbleId"], "bid-img")

    async def test_unknown_bubble_type_is_rejected_not_acked(self):
        bubble = _image_bubble("bid-weird")
        bubble["type"] = 9
        resp = await self.client.post("/bubble", json=bubble)
        self.assertEqual(resp.status, 400)
        self.assertIn("receive bubble failed", (await resp.text()))

    async def test_file_bubble_still_works(self):
        bubble = _image_bubble("bid-file")
        bubble["type"] = model.BUBBLE_FILE
        resp = await self.client.post("/bubble", json=bubble)
        self.assertEqual(resp.status, 200)
        row = self.store.con.execute(
            "SELECT type FROM bubble_entities WHERE id='bid-file'").fetchone()
        self.assertEqual(row[0], model.BUBBLE_FILE)


if __name__ == "__main__":
    unittest.main()
