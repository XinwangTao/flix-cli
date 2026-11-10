"""Durable text outbox: persist first, retry, and only 200 counts as sent."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flixcli import outbox
from flixcli.channel import PeerError
from flixcli.config import Store

PEER = "peer-0000-1111-2222-333333333333"
ME = "me-0000-1111-2222-333333333333"


class OutboxStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "relaydesk.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_roundtrip_states(self):
        self.store.add_outbox("o1", PEER, "text", "{}")
        self.assertEqual([r["id"] for r in self.store.outbox_pending()], ["o1"])
        self.store.mark_outbox("o1", "delivered")
        self.assertEqual(self.store.outbox_pending(), [])
        self.assertEqual(self.store.outbox_get("o1")["state"], "delivered")
        self.assertTrue(self.store.outbox_get("o1")["deliveredAt"])

    def test_expire_marks_old_rows(self):
        self.store.add_outbox("old", PEER, "text", "{}")
        self.store.con.execute("UPDATE outbox SET created=1 WHERE id='old'")
        self.store.con.commit()
        self.assertEqual(self.store.outbox_expire(), 1)
        self.assertEqual(self.store.outbox_get("old")["state"], "expired")

    def test_retry_and_drop(self):
        self.store.add_outbox("o2", PEER, "text", "{}")
        self.store.mark_outbox("o2", "failed", "boom")
        self.assertTrue(self.store.outbox_retry("o2"))
        self.assertEqual(self.store.outbox_get("o2")["state"], "queued")
        self.assertTrue(self.store.outbox_drop("o2"))
        self.assertEqual(self.store.outbox_get("o2")["state"], "dropped")


class SendTextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "relaydesk.db")
        self.store.upsert_device(PEER, alias="peer", ip="127.0.0.1", port=8891)
        self.ident = {"fingerprint": ME, "alias": "me"}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def test_unreachable_peer_queues_instead_of_losing(self):
        with mock.patch.object(outbox, "post_bubble",
                               side_effect=PeerError("unreachable", "boom")):
            result = await outbox.send_text(self.store, self.ident, PEER, "hi")
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "queued")
        self.assertEqual(len(self.store.outbox_pending()), 1)

    async def test_rejected_is_permanent(self):
        with mock.patch.object(outbox, "post_bubble",
                               side_effect=PeerError("rejected", "HTTP 403")):
            result = await outbox.send_text(self.store, self.ident, PEER, "hi")
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.store.outbox_pending(), [])

    async def test_ack_marks_delivered(self):
        with mock.patch.object(outbox, "post_bubble",
                               new=mock.AsyncMock(return_value="received bubble")):
            result = await outbox.send_text(self.store, self.ident, PEER, "hi")
        self.assertEqual(result["state"], "delivered")
        self.assertEqual(self.store.outbox_pending(), [])

    async def test_flush_retries_queued_rows(self):
        self.store.add_outbox(
            "q1", PEER, "text",
            json.dumps({"id": "q1", "from": ME, "to": PEER, "type": 0,
                        "content": "later", "time": 1, "groupId": ""}))
        with mock.patch.object(outbox, "post_bubble",
                               new=mock.AsyncMock(return_value="received bubble")):
            result = await outbox.flush(self.store)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(self.store.outbox_get("q1")["state"], "delivered")

    async def test_flush_keeps_unacked_rows_queued(self):
        self.store.add_outbox(
            "q2", PEER, "text",
            json.dumps({"id": "q2", "from": ME, "to": PEER, "type": 0,
                        "content": "later", "time": 1, "groupId": ""}))
        with mock.patch.object(outbox, "post_bubble",
                               new=mock.AsyncMock(return_value="weird reply")):
            result = await outbox.flush(self.store)
        self.assertEqual(result["sent"], 0)
        row = self.store.outbox_get("q2")
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["attempts"], 1)

    async def test_flush_resolves_ip_keyed_row_to_announced_peer(self):
        """A message queued towards a bare IP must land once that peer announces.

        Regression: `send-text <ip>` to a peer that was not yet in the device
        table stored the address as the outbox peer. flush() looked it up with
        device_ip(peer), which only matches fingerprints, so the row was
        "skipped" every 30 s until the 7-day TTL -- even though the peer had
        been announcing the whole time. It also left `to` as an IP, where the
        protocol expects a fingerprint.
        """
        address = "192.168.137.162"
        self.store.add_outbox(
            "q3", address, "text",
            json.dumps({"id": "q3", "from": ME, "to": address, "type": 0,
                        "content": "later", "time": 1, "groupId": ""}))
        # The peer announces after the message was queued, on a non-default port.
        self.store.upsert_device(PEER, alias="peer", ip=address, port=18991)
        posted = mock.AsyncMock(return_value="received bubble")
        with mock.patch.object(outbox, "post_bubble", new=posted):
            result = await outbox.flush(self.store)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["skipped"], 0)
        row = self.store.outbox_get("q3")
        self.assertEqual(row["state"], "delivered")
        self.assertEqual(row["peer"], PEER)
        bubble = posted.await_args.args[1]
        self.assertEqual(bubble["to"], PEER)
        self.assertEqual(posted.await_args.kwargs["port"], 18991)

    async def test_flush_skips_row_that_is_not_an_address_or_peer(self):
        """Neither a known peer nor an address: keep the message, never guess."""
        self.store.add_outbox(
            "q4", "not-a-peer", "text",
            json.dumps({"id": "q4", "from": ME, "to": "not-a-peer", "type": 0,
                        "content": "later", "time": 1, "groupId": ""}))
        posted = mock.AsyncMock(return_value="received bubble")
        with mock.patch.object(outbox, "post_bubble", new=posted):
            result = await outbox.flush(self.store)
        self.assertEqual(result["skipped"], 1)
        posted.assert_not_awaited()
        self.assertEqual(self.store.outbox_get("q4")["state"], "queued")


if __name__ == "__main__":
    unittest.main()
