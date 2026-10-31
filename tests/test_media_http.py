"""Two-node HTTP integration for the approved media session protocol."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web

try:
    import aiortc  # noqa: F401
except ImportError:  # 媒体是可选依赖（[media] extra），基础环境里跳过
    raise unittest.SkipTest("media extra not installed")

from flixcli.config import Store
from flixcli.media_session import SessionManager
from flixcli.media_transport import MediaRuntime
from flixcli.server import make_app


class MediaHttpTests(unittest.TestCase):
    def test_request_approve_and_authenticated_offer(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                a_fp, b_fp = "a" * 36, "b" * 36
                a_store = Store(Path(tmp) / "a.sqlite")
                b_store = Store(Path(tmp) / "b.sqlite")
                for store, peer, port in ((a_store, b_fp, 19492), (b_store, a_fp, 19491)):
                    store.upsert_device(peer, ip="127.0.0.1", port=port)
                    store.add_pair(peer, "0000")
                ma = SessionManager(a_fp, a_store.is_paired, lambda _fp: "127.0.0.1")
                mb = SessionManager(b_fp, b_store.is_paired, lambda _fp: "127.0.0.1")
                ra = MediaRuntime(ma, Path(tmp) / "frames-a")
                rb = MediaRuntime(mb, Path(tmp) / "frames-b")
                runners = []
                for store, ident, manager, runtime, port in (
                    (a_store, {"fingerprint": a_fp}, ma, ra, 19491),
                    (b_store, {"fingerprint": b_fp}, mb, rb, 19492),
                ):
                    app = make_app(store, ident, lambda *_: None,
                                   device_port=port, media_manager=manager,
                                   media_runtime=runtime)
                    runner = web.AppRunner(app)
                    await runner.setup()
                    await web.TCPSite(runner, "127.0.0.1", port).start()
                    runners.append(runner)
                try:
                    request = ma.request(b_fp, ["view"])
                    async with aiohttp.ClientSession() as client:
                        async with client.post("http://127.0.0.1:19492/webrtc/v1/session",
                                               json={"action": "request", "from": a_fp,
                                                     "callId": request["callId"],
                                                     "capabilities": ["view"],
                                                     "publicKey": request["publicKey"]}) as response:
                            self.assertEqual(response.status, 200)
                    decision = mb.approve(request["callId"], ["view"])
                    ma.accept_decision(request["callId"], decision["approved"],
                                       decision["publicKey"], decision["expiresAt"] / 1000)
                    offer_peer = ra.create(request["callId"])
                    offer_peer.add_control_channel()
                    offer = await offer_peer.offer()
                    envelope = ma.next_signal(request["callId"], "offer", offer)
                    async with aiohttp.ClientSession() as client:
                        async with client.post("http://127.0.0.1:19492/webrtc/v1/signal",
                                               json=envelope) as response:
                            body = await response.json()
                            self.assertEqual(response.status, 200, body)
                    answer = body["signal"]
                    ma.verify_signal(answer, source_ip="127.0.0.1")
                    await offer_peer.apply_answer(answer["payload"])
                    await asyncio.sleep(0.3)
                    self.assertIn(ma.require(request["callId"]).state,
                                  ("connecting", "active"))
                    await offer_peer.close()
                    await rb.get(request["callId"]).close()
                    await asyncio.sleep(0.05)
                finally:
                    for runner in runners:
                        await runner.cleanup()
                    a_store.close(); b_store.close()
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
