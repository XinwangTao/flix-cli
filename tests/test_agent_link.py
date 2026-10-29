"""Agent-to-agent channel: queueing, the pairing gate, and long-poll inbox."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
import uuid
from pathlib import Path

from aiohttp import ClientSession, web

from flixcli import model
from flixcli.agentlink import flush_outbox, inbox, send_agent_message
from flixcli.config import Store
from flixcli.server import make_app

ME = {"fingerprint": "11111111-1111-1111-1111-111111111111", "alias": "A",
      "deviceModel": "Windows"}
PEER = {"fingerprint": "22222222-2222-2222-2222-222222222222", "alias": "B",
        "deviceModel": "Windows"}


def make_store(root: Path, ident: dict) -> Store:
    store = Store(root / "db.sqlite")
    store.upsert_device(ident["fingerprint"], alias=ident["alias"],
                        ip="127.0.0.1", port=19999)
    return store


class AgentIntentTests(unittest.TestCase):
    def test_unpaired_sender_is_rejected_and_nothing_is_stored(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = make_store(root, PEER)
                app = make_app(store, PEER, lambda *_: None)
                runner = web.AppRunner(app); await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", 19631).start()
                try:
                    intent = model.agent_intent(ME["fingerprint"], PEER["fingerprint"],
                                                str(uuid.uuid4()), "偷偷说话")
                    async with ClientSession() as client:
                        r = await client.post("http://127.0.0.1:19631/intent", json=intent)
                        self.assertEqual(r.status, 403)
                    self.assertEqual(store.agent_inbox(), [])
                finally:
                    await runner.cleanup(); store.close()
        asyncio.run(run())

    def test_paired_sender_lands_in_the_inbox(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = make_store(root, PEER)
                store.add_pair(ME["fingerprint"], "3012")
                app = make_app(store, PEER, lambda *_: None)
                runner = web.AppRunner(app); await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", 19632).start()
                try:
                    mid = str(uuid.uuid4())
                    intent = model.agent_intent(ME["fingerprint"], PEER["fingerprint"],
                                                mid, "在吗", "", 1_700_000_000_000)
                    async with ClientSession() as client:
                        r = await client.post("http://127.0.0.1:19632/intent", json=intent)
                        self.assertEqual(r.status, 200)
                        self.assertEqual((await r.text()).strip(), "{0: success}")
                    messages = store.agent_inbox()
                    self.assertEqual(len(messages), 1)
                    self.assertEqual(messages[0]["id"], mid)
                    self.assertEqual(messages[0]["body"], "在吗")
                    self.assertEqual(messages[0]["from"], ME["fingerprint"])
                finally:
                    await runner.cleanup(); store.close()
        asyncio.run(run())

    def test_empty_body_is_rejected(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                store = make_store(Path(tmp), PEER)
                store.add_pair(ME["fingerprint"], "3012")
                app = make_app(store, PEER, lambda *_: None)
                runner = web.AppRunner(app); await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", 19633).start()
                try:
                    intent = model.agent_intent(ME["fingerprint"], PEER["fingerprint"],
                                                str(uuid.uuid4()), "")
                    async with ClientSession() as client:
                        r = await client.post("http://127.0.0.1:19633/intent", json=intent)
                        self.assertEqual(r.status, 400)
                finally:
                    await runner.cleanup(); store.close()
        asyncio.run(run())


class SendTests(unittest.TestCase):
    def test_send_delivers_to_a_reachable_peer(self):
        async def run():
            with tempfile.TemporaryDirectory() as mine_tmp, \
                    tempfile.TemporaryDirectory() as peer_tmp:
                mine = make_store(Path(mine_tmp), ME)
                peer = make_store(Path(peer_tmp), PEER)
                peer.add_pair(ME["fingerprint"], "3012")
                app = make_app(peer, PEER, lambda *_: None)
                runner = web.AppRunner(app); await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", 19634).start()
                mine.upsert_device(PEER["fingerprint"], alias="B",
                                   ip="127.0.0.1", port=19634)
                try:
                    result = await send_agent_message(mine, ME, PEER["fingerprint"],
                                                      "你好，B")
                    self.assertEqual(result["state"], "delivered")
                    self.assertEqual(peer.agent_inbox()[0]["body"], "你好，B")
                finally:
                    await runner.cleanup(); peer.close(); mine.close()
        asyncio.run(run())

    def test_unreachable_peer_queues_then_flush_delivers(self):
        async def run():
            with tempfile.TemporaryDirectory() as mine_tmp, \
                    tempfile.TemporaryDirectory() as peer_tmp:
                mine = make_store(Path(mine_tmp), ME)
                peer = make_store(Path(peer_tmp), PEER)
                peer.add_pair(ME["fingerprint"], "3012")
                mine.upsert_device(PEER["fingerprint"], alias="B",
                                   ip="127.0.0.1", port=19635)
                try:
                    result = await send_agent_message(mine, ME, PEER["fingerprint"],
                                                      "离线时排队")
                    self.assertEqual(result["state"], "queued")
                    self.assertEqual(len(mine.pending_agent_outbox()), 1)

                    app = make_app(peer, PEER, lambda *_: None)
                    runner = web.AppRunner(app); await runner.setup()
                    await web.TCPSite(runner, "127.0.0.1", 19635).start()
                    try:
                        flushed = await flush_outbox(mine, ME)
                        self.assertEqual(flushed["sent"], 1)
                        self.assertEqual(mine.pending_agent_outbox(), [])
                        self.assertEqual(peer.agent_inbox()[0]["body"], "离线时排队")
                    finally:
                        await runner.cleanup()
                finally:
                    peer.close(); mine.close()
        asyncio.run(run())

    def test_unknown_target_is_an_error_not_a_queue(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                mine = make_store(Path(tmp), ME)
                try:
                    result = await send_agent_message(mine, ME, "nobody-here", "hi")
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["error"], "unknown-target")
                    self.assertEqual(mine.pending_agent_outbox(), [])
                finally:
                    mine.close()
        asyncio.run(run())


class InboxTests(unittest.TestCase):
    def test_fetch_marks_read_so_it_is_delivered_once(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                store = make_store(Path(tmp), PEER)
                store.add_agent_message("m1", "in", ME["fingerprint"], "一")
                store.add_agent_message("m2", "in", ME["fingerprint"], "二")
                try:
                    first = await inbox(store)
                    self.assertEqual(first["count"], 2)
                    second = await inbox(store)
                    self.assertEqual(second["count"], 0)
                    self.assertEqual(len(store.agent_inbox(unread_only=False)), 2)
                finally:
                    store.close()
        asyncio.run(run())

    def test_long_poll_times_out_quickly_when_empty(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                store = make_store(Path(tmp), PEER)
                try:
                    loop = asyncio.get_event_loop()
                    started = loop.time()
                    result = await inbox(store, wait_seconds=0.6)
                    self.assertEqual(result["count"], 0)
                    self.assertGreaterEqual(loop.time() - started, 0.5)
                finally:
                    store.close()
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
