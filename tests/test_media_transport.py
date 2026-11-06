"""Loopback WebRTC offer/answer test without camera or microphone access."""
from __future__ import annotations

import asyncio
import contextlib
import sys
import unittest

from flixcli.media_session import SessionManager
from flixcli.media_transport import PeerMedia, available


@contextlib.contextmanager
def _selector_loop():
    # See tests/test_media_http.py: Proactor's UDP sendto wedges on this host.
    if sys.platform != "win32":
        yield
        return
    old = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        yield
    finally:
        asyncio.set_event_loop_policy(old)


class MediaTransportTests(unittest.TestCase):
    def test_loopback_offer_answer_signaling(self):
        if not available():
            self.skipTest("media extra not installed")
        async def run():
            a = "a" * 36
            b = "b" * 36
            clock = lambda: 1000.0
            ips = {a: "127.0.0.1", b: "127.0.0.1"}
            paired = lambda fp: fp in ips
            ma = SessionManager(a, paired, ips.__getitem__, clock)
            mb = SessionManager(b, paired, ips.__getitem__, clock)
            request = ma.request(b, ["view"])
            incoming = mb.request(a, ["view"], incoming=True,
                                  call_id=request["callId"], source_ip="127.0.0.1",
                                  peer_public_key=request["publicKey"])
            decision = mb.approve(incoming["callId"], ["view"])
            ma.accept_decision(request["callId"], decision["approved"],
                               decision["publicKey"], decision["expiresAt"] / 1000)
            offer_peer = PeerMedia(request["callId"], ma)
            answer_peer = PeerMedia(request["callId"], mb)
            offer_peer.add_control_channel()
            offer = await offer_peer.offer()
            answer = await answer_peer.answer(offer, share_screen=False)
            await offer_peer.apply_answer(answer)
            self.assertIn("a=ice-ufrag:", offer["sdp"])
            self.assertIn("a=ice-ufrag:", answer["sdp"])
            await asyncio.sleep(0.2)
            await offer_peer.close()
            await answer_peer.close()
            await asyncio.sleep(0.05)
        with _selector_loop():
            asyncio.run(asyncio.wait_for(run(), timeout=30))


if __name__ == "__main__":
    unittest.main()
