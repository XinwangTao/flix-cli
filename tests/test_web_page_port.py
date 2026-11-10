"""Web page port: device refresh, upload details, group errors, WS PIN.

The v2.4.0 security rewrite kept the PIN throttle and upload-name gate but lost
the client-side feedback and the group-list text rendering; pin those down.
"""
from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from flixcli.config import Store
from flixcli.webapp import make_web_app

ME = {"fingerprint": "self-fp", "alias": "me", "deviceModel": "Windows"}
EVIL = '<img src=x onerror="fetch(\'/send\')">'


class WebPagePortTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "case"
        self.root.mkdir()
        self.store = Store(self.root / "db.sqlite")
        self.store.set_pref("saveDir", str(self.root / "downloads"))
        self.gid = str(uuid.uuid4())
        self.store.merge_group_sync(self.gid, EVIL, ["fp-peer"], 1)
        self.store.upsert_device("fp-peer", alias="peer", ip="127.0.0.1",
                                 port=8891)
        app, _push = make_web_app(
            self.store, ME, lambda *_: None,
            lambda: str(self.root / "downloads"), 8891)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.pin = self.store.own_web_pin()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self._tmp.cleanup()

    async def test_send_page_refreshes_devices_and_shows_upload_details(self):
        r = await self.client.get(f"/{self.pin}")
        self.assertEqual(r.status, 200)
        page = await r.text()
        self.assertIn("async function loadDevices", page)
        self.assertIn("setInterval(loadDevices, 10000)", page)
        self.assertIn("sel.value = current", page)
        self.assertIn("async function uploadOne", page)
        self.assertIn("saved.forwardError", page)
        self.assertIn("saved.name !== f.name", page)
        self.assertIn("/ws?pin=", page)
        self.assertIn('id="serr"', page)
        self.assertIn("if(!r.ok || j.ok===false)", page)
        self.assertIn("发送失败：", page)
        self.assertIn("ws.readyState!==WebSocket.OPEN", page)
        self.assertIn("err.textContent='';add(v,true,'已送达');i.value='';", page)

    async def test_group_list_uses_text_nodes_and_reports_errors(self):
        r = await self.client.get(f"/group?pin={self.pin}")
        self.assertEqual(r.status, 200)
        page = await r.text()
        self.assertNotIn("innerHTML", page)
        self.assertIn("createTextNode(g.name)", page)
        self.assertIn("encodeURIComponent(g.id)", page)
        self.assertIn("加载失败：", page)

    async def test_group_page_reports_poll_and_send_errors(self):
        r = await self.client.get(f"/group/{self.gid}?pin={self.pin}")
        self.assertEqual(r.status, 200)
        page = await r.text()
        self.assertIn('id="gerr"', page)
        self.assertIn("刷新失败：", page)
        self.assertIn("发送失败：", page)
        self.assertIn("if (!r.ok) throw", page)

    async def test_group_json_rejects_a_wrong_pin(self):
        wrong = "0000" if self.pin != "0000" else "0001"
        r = await self.client.get(f"/group/json?pin={wrong}")
        self.assertEqual(r.status, 401)
        self.assertEqual((await r.json())["error"], "unauthorized")

    async def test_group_data_returns_bubble_ids(self):
        # Two bubbles sharing one millisecond: a time-only cursor loses the
        # second one on the next poll, so the payload must carry an id.
        self.store.add_text_bubble("b-1", "fp-peer", "self-fp", "一",
                                   5000, self.gid)
        self.store.add_text_bubble("b-2", "self-fp", "fp-peer", "二",
                                   5000, self.gid)
        r = await self.client.get(
            f"/group/{self.gid}/data?pin={self.pin}&after=4999")
        self.assertEqual(r.status, 200)
        items = (await r.json())["items"]
        self.assertEqual({it["id"] for it in items}, {"b-1", "b-2"})
        self.assertEqual({it["time"] for it in items}, {5000})

    async def test_group_page_dedupes_by_id_and_resends_last_millisecond(self):
        r = await self.client.get(f"/group/{self.gid}?pin={self.pin}")
        page = await r.text()
        self.assertIn("const seen = new Set();", page)
        self.assertIn("Math.max(0, after - 1)", page)
        self.assertIn("if (seen.has(it.id)) continue;", page)
        self.assertIn("seen.add(it.id);", page)

    async def test_upload_forward_failure_is_reported_in_saved_entry(self):
        form = FormData()
        form.add_field("to", "fp-peer")
        form.add_field("file", b"data", filename="f.bin",
                       content_type="application/octet-stream")
        with mock.patch("flixcli.files.send_file",
                        side_effect=RuntimeError("boom")):
            r = await self.client.post(f"/upload?pin={self.pin}", data=form)
        self.assertEqual(r.status, 200)
        saved = (await r.json())["saved"][0]
        self.assertFalse(saved["forwarded"])
        self.assertIn("boom", saved["forwardError"])


class WebSendFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "case"
        self.root.mkdir()
        self.store = Store(self.root / "db.sqlite")
        self.store.set_pref("saveDir", str(self.root / "downloads"))
        self.pin = self.store.own_web_pin()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self._tmp.cleanup()

    async def _start(self, sender):
        app, _push = make_web_app(
            self.store, ME, lambda *_: None,
            lambda: str(self.root / "downloads"), 8891, send_text_fn=sender)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def test_sender_exception_is_reported_as_json(self):
        async def boom(to, text):
            raise RuntimeError("transport down")

        await self._start(boom)
        r = await self.client.post("/send", json={
            "pin": self.pin, "to": "peer-fp", "text": "hi"})
        self.assertEqual(r.status, 502)
        body = await r.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "send-failed")
        self.assertIn("transport down", body["detail"])

    async def test_successful_sender_result_is_passed_through(self):
        async def ok(to, text):
            return {"ok": True, "to": to, "queued": text}

        await self._start(ok)
        r = await self.client.post("/send", json={
            "pin": self.pin, "to": "peer-fp", "text": "hi"})
        self.assertEqual(r.status, 200)
        self.assertEqual(await r.json(),
                         {"ok": True, "to": "peer-fp", "queued": "hi"})


if __name__ == "__main__":
    unittest.main()
