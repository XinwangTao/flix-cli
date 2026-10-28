"""Security and lifecycle tests for approved LAN media sessions."""
from __future__ import annotations

import unittest

from flixcli.media_session import ACTIVE_TTL, SessionError, SessionManager


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class MediaSessionTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.a = "a" * 36
        self.b = "b" * 36
        ips = {self.a: "10.0.0.1", self.b: "10.0.0.2"}
        paired = lambda fp: fp in ips
        lookup = lambda fp: ips.get(fp)
        self.ma = SessionManager(self.a, paired, lookup, self.clock)
        self.mb = SessionManager(self.b, paired, lookup, self.clock)

    def approved_pair(self):
        outgoing = self.ma.request(self.b, ["view", "control"])
        incoming = self.mb.request(self.a, ["view", "control"], incoming=True,
                                   call_id=outgoing["callId"], source_ip="10.0.0.1",
                                   peer_public_key=outgoing["publicKey"])
        decision = self.mb.approve(incoming["callId"], ["view", "control"])
        self.ma.accept_decision(outgoing["callId"], decision["approved"],
                                decision["publicKey"], decision["expiresAt"] / 1000)
        return outgoing["callId"]

    def test_key_exchange_and_authenticated_signal(self):
        call_id = self.approved_pair()
        self.assertEqual(self.ma.require(call_id).token, self.mb.require(call_id).token)
        envelope = self.ma.sign(call_id, self.a, self.b, "offer", {"sdp": "x"}, 1)
        session = self.mb.verify_signal(envelope, source_ip="10.0.0.1")
        self.assertEqual(session.call_id, call_id)
        with self.assertRaisesRegex(SessionError, "replay"):
            self.mb.verify_signal(envelope, source_ip="10.0.0.1")

    def test_tamper_source_and_sequence_are_rejected(self):
        call_id = self.approved_pair()
        envelope = self.ma.sign(call_id, self.a, self.b, "offer", {"sdp": "x"}, 1)
        with self.assertRaisesRegex(SessionError, "source IP"):
            self.mb.verify_signal(envelope, source_ip="10.0.0.99")
        broken = dict(envelope)
        broken["payload"] = {"sdp": "tampered"}
        with self.assertRaisesRegex(SessionError, "bad-auth"):
            self.mb.verify_signal(broken, source_ip="10.0.0.1")

    def test_request_requires_pair_and_view(self):
        with self.assertRaisesRegex(SessionError, "paired"):
            self.ma.request("unknown", ["view"])
        with self.assertRaisesRegex(SessionError, "view is required"):
            self.ma.request(self.b, ["control"])

    def test_only_receiver_approves_requested_capabilities(self):
        outgoing = self.ma.request(self.b, ["view"])
        incoming = self.mb.request(self.a, ["view"], incoming=True,
                                   call_id=outgoing["callId"], source_ip="10.0.0.1",
                                   peer_public_key=outgoing["publicKey"])
        with self.assertRaisesRegex(SessionError, "requested"):
            self.mb.approve(incoming["callId"], ["view", "control"])
        with self.assertRaisesRegex(SessionError, "receiver"):
            self.ma.approve(outgoing["callId"], ["view"])

    def test_expiry_clears_active_token(self):
        call_id = self.approved_pair()
        self.clock.now += ACTIVE_TTL + 1
        self.assertEqual(self.ma.require(call_id).state, "expired")
        self.assertEqual(self.ma.require(call_id).token, "")

    def test_control_requires_active_and_approval(self):
        call_id = self.approved_pair()
        with self.assertRaisesRegex(SessionError, "control-not-approved"):
            self.mb.handle_control(call_id, {"v": 1, "seq": 1, "kind": "pointer",
                                             "x": 0.2, "y": 0.3})


if __name__ == "__main__":
    unittest.main()
