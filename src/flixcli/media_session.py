"""Authenticated, approval-gated LAN media sessions.

The signaling envelope is flixcli-specific. Official Flix media compatibility
is not claimed. A session is bound to two fingerprints, expires, rejects replay
and only gains capabilities explicitly approved on the receiving machine.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .control import ControlError, RateGate, apply_event, validate_event

VALID_CAPABILITIES = frozenset({"view", "audio", "control"})
VALID_SIGNAL_TYPES = frozenset({"offer", "answer", "ice", "hangup"})
MAX_SIGNAL_BYTES = 64 * 1024
PENDING_TTL = 60.0
ACTIVE_TTL = 2 * 60 * 60.0


class SessionError(ValueError):
    def __init__(self, kind: str, detail: str = ""):
        self.kind = kind
        self.detail = detail or kind
        super().__init__(f"{kind}: {self.detail}")


@dataclass
class MediaSession:
    call_id: str
    initiator: str
    receiver: str
    requested: frozenset[str]
    created_at: float
    state: str = "pending"
    approved: frozenset[str] = field(default_factory=frozenset)
    token: str = ""
    private_key: object | None = None
    peer_public_key: str = ""
    expires_at: float = 0.0
    last_seq: dict[str, int] = field(default_factory=dict)
    outbound_seq: int = 0
    nonces: set[str] = field(default_factory=set)
    control_seq: int = -1
    control_gate: RateGate = field(default_factory=RateGate)
    reason: str = ""

    def public(self) -> dict:
        return {
            "callId": self.call_id, "initiator": self.initiator,
            "receiver": self.receiver, "requested": sorted(self.requested),
            "approved": sorted(self.approved), "state": self.state,
            "createdAt": int(self.created_at * 1000),
            "expiresAt": int(self.expires_at * 1000) if self.expires_at else None,
            "reason": self.reason or None,
        }


class SessionManager:
    def __init__(self, own_fp: str, is_paired: Callable[[str], bool],
                 peer_address: Callable[[str], str | None] | None = None,
                 clock: Callable[[], float] = time.time):
        self.own_fp = own_fp
        self.is_paired = is_paired
        self.peer_address = peer_address or (lambda _fp: None)
        self.clock = clock
        self.sessions: dict[str, MediaSession] = {}
        self.listeners: list[Callable[[dict], None]] = []
        self.media: dict[str, object] = {}
        self.audit: list[dict] = []

    def on_event(self, fn: Callable[[dict], None]) -> None:
        self.listeners.append(fn)

    def _emit(self, session: MediaSession, event: str) -> None:
        payload = {"type": "mediaSessionChanged", "event": event,
                   "session": session.public()}
        self.audit.append({"time": int(self.clock() * 1000), "event": event,
                           "callId": session.call_id, "peer": self.peer(session),
                           "state": session.state,
                           "capabilities": sorted(session.approved),
                           "reason": session.reason or None})
        self.audit[:] = self.audit[-200:]
        for listener in self.listeners:
            try:
                listener(payload)
            except Exception:
                pass

    def peer(self, session: MediaSession) -> str:
        return session.receiver if session.initiator == self.own_fp else session.initiator

    def _purge(self) -> None:
        now = self.clock()
        for session in list(self.sessions.values()):
            limit = session.expires_at or session.created_at + PENDING_TTL
            if session.state not in ("closed", "denied", "expired") and now > limit:
                session.state = "expired"
                session.reason = "session expired"
                session.token = ""
                self._emit(session, "expired")

    @staticmethod
    def _new_key() -> tuple[object, str]:
        private = X25519PrivateKey.generate()
        public = base64.urlsafe_b64encode(private.public_key().public_bytes_raw()).decode().rstrip("=")
        return private, public

    @staticmethod
    def _derive(private, peer_public: str, call_id: str) -> str:
        try:
            raw = base64.urlsafe_b64decode(peer_public + "=" * (-len(peer_public) % 4))
            shared = private.exchange(X25519PublicKey.from_public_bytes(raw))
        except (ValueError, TypeError) as exc:
            raise SessionError("bad-public-key") from exc
        key = HKDF(algorithm=hashes.SHA256(), length=32,
                   salt=call_id.encode("utf-8"), info=b"flix-media-v1").derive(shared)
        return base64.urlsafe_b64encode(key).decode().rstrip("=")

    def request(self, peer: str, capabilities: list[str], *, incoming: bool = False,
                call_id: str | None = None, source_ip: str | None = None,
                peer_public_key: str = "") -> dict:
        self._purge()
        if not peer or not self.is_paired(peer):
            raise SessionError("not-paired", "remote media requires a paired peer")
        expected_ip = self.peer_address(peer)
        if incoming and expected_ip and source_ip and expected_ip != source_ip:
            raise SessionError("source-mismatch", "source IP does not match paired device")
        requested = frozenset(capabilities)
        if not requested or not requested <= VALID_CAPABILITIES or "view" not in requested:
            raise SessionError("bad-capabilities", "view is required; capabilities are view/audio/control")
        call_id = call_id or str(uuid.uuid4())
        if call_id in self.sessions:
            raise SessionError("duplicate-call", "callId already exists")
        initiator, receiver = (peer, self.own_fp) if incoming else (self.own_fp, peer)
        private_key, public_key = self._new_key()
        session = MediaSession(call_id, initiator, receiver, requested, self.clock(),
                               private_key=private_key, peer_public_key=peer_public_key)
        if peer_public_key:
            session.token = self._derive(private_key, peer_public_key, call_id)
        self.sessions[call_id] = session
        self._emit(session, "requested")
        session_public = session.public()
        session_public["publicKey"] = public_key
        return session_public

    def approve(self, call_id: str, capabilities: list[str]) -> dict:
        session = self.require(call_id)
        if session.receiver != self.own_fp or session.state != "pending":
            raise SessionError("not-approvable", "only the local receiver can approve a pending call")
        approved = frozenset(capabilities)
        if not approved <= session.requested or "view" not in approved:
            raise SessionError("bad-capabilities", "approval must include view and be requested")
        if not session.peer_public_key:
            raise SessionError("bad-public-key", "request did not carry a public key")
        session.approved = approved
        session.state = "approved"
        session.token = self._derive(session.private_key, session.peer_public_key, call_id)
        session.expires_at = self.clock() + ACTIVE_TTL
        self._emit(session, "approved")
        response = session.public()
        response["publicKey"] = base64.urlsafe_b64encode(
            session.private_key.public_key().public_bytes_raw()).decode().rstrip("=")
        return response

    def accept_decision(self, call_id: str, approved: list[str], peer_public_key: str,
                        expires_at: float) -> dict:
        session = self.require(call_id)
        if session.initiator != self.own_fp or session.state != "pending":
            raise SessionError("bad-decision", "only the local initiator accepts a pending decision")
        capabilities = frozenset(approved)
        if not capabilities <= session.requested or "view" not in capabilities:
            raise SessionError("bad-capabilities")
        if not isinstance(peer_public_key, str) or not peer_public_key:
            raise SessionError("bad-public-key")
        if expires_at <= self.clock() or expires_at > self.clock() + ACTIVE_TTL + 60:
            raise SessionError("bad-expiry")
        session.peer_public_key = peer_public_key
        session.token = self._derive(session.private_key, peer_public_key, call_id)
        session.approved = capabilities
        session.expires_at = expires_at
        session.state = "approved"
        self._emit(session, "decision-accepted")
        return session.public()

    def accept_denial(self, call_id: str, reason: str = "denied by peer") -> dict:
        session = self.require(call_id)
        if session.initiator != self.own_fp or session.state != "pending":
            raise SessionError("bad-decision")
        session.state, session.reason, session.token = "denied", reason[:160], ""
        self._emit(session, "decision-denied")
        return session.public()

    def deny(self, call_id: str, reason: str = "denied by user") -> dict:
        session = self.require(call_id)
        if session.receiver != self.own_fp or session.state != "pending":
            raise SessionError("not-deniable", "only the local receiver can deny a pending call")
        session.state, session.reason, session.token = "denied", reason[:160], ""
        self._emit(session, "denied")
        return session.public()

    def revoke(self, call_id: str, reason: str = "revoked") -> dict:
        session = self.require(call_id)
        session.state, session.reason, session.token = "closed", reason[:160], ""
        media = self.media.pop(call_id, None)
        if media is not None:
            close = getattr(media, "close", None)
            if close:
                result = close()
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
        self._emit(session, "closed")
        return session.public()

    def require(self, call_id: str) -> MediaSession:
        self._purge()
        session = self.sessions.get(call_id)
        if session is None:
            raise SessionError("no-session", "unknown callId")
        return session

    def list(self) -> list[dict]:
        self._purge()
        return [s.public() for s in self.sessions.values()]

    @staticmethod
    def _canonical(envelope: dict) -> bytes:
        body = {k: envelope.get(k) for k in
                ("v", "callId", "from", "to", "type", "seq", "nonce", "payload")}
        return json.dumps(body, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")

    def next_signal(self, call_id: str, signal_type: str, payload: dict) -> dict:
        session = self.require(call_id)
        session.outbound_seq += 1
        return self.sign(call_id, self.own_fp, self.peer(session), signal_type,
                         payload, session.outbound_seq)

    def sign(self, call_id: str, sender: str, receiver: str, signal_type: str,
             payload: dict, seq: int, nonce: str | None = None) -> dict:
        session = self.require(call_id)
        if not session.token:
            raise SessionError("not-approved", "session has no active token")
        envelope = {"v": 1, "callId": call_id, "from": sender, "to": receiver,
                    "type": signal_type, "seq": seq,
                    "nonce": nonce or secrets.token_urlsafe(12), "payload": payload}
        envelope["auth"] = hmac.new(session.token.encode(), self._canonical(envelope),
                                      hashlib.sha256).hexdigest()
        return envelope

    def verify_signal(self, envelope: dict, source_ip: str | None = None) -> MediaSession:
        if len(json.dumps(envelope, ensure_ascii=False).encode("utf-8")) > MAX_SIGNAL_BYTES:
            raise SessionError("signal-too-large")
        session = self.require(str(envelope.get("callId", "")))
        if session.state not in ("approved", "connecting", "active") or not session.token:
            raise SessionError("not-approved")
        sender, receiver = envelope.get("from"), envelope.get("to")
        if {sender, receiver} != {session.initiator, session.receiver} or receiver != self.own_fp:
            raise SessionError("peer-mismatch")
        expected_ip = self.peer_address(str(sender))
        if expected_ip and source_ip and expected_ip != source_ip:
            raise SessionError("source-mismatch", "source IP does not match paired device")
        if envelope.get("v") != 1 or envelope.get("type") not in VALID_SIGNAL_TYPES:
            raise SessionError("bad-signal", "unsupported signal version or type")
        seq, nonce = envelope.get("seq"), envelope.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            raise SessionError("bad-nonce", "nonce is required")
        if nonce in session.nonces:
            raise SessionError("replay", "nonce was already accepted")
        if not isinstance(seq, int) or seq <= session.last_seq.get(str(sender), -1):
            raise SessionError("bad-sequence", "signal sequence must increase")
        supplied = str(envelope.get("auth", ""))
        expected = hmac.new(session.token.encode(), self._canonical(envelope),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            raise SessionError("bad-auth")
        session.last_seq[str(sender)] = seq
        session.nonces.add(nonce)
        if len(session.nonces) > 512:
            session.nonces = set(list(session.nonces)[-256:])
        return session

    def handle_control(self, call_id: str, event: dict) -> dict:
        session = self.require(call_id)
        if session.state != "active" or "control" not in session.approved:
            raise SessionError("control-not-approved")
        if not session.control_gate.allow():
            raise SessionError("control-rate-limit")
        try:
            clean = validate_event(event, session.control_seq)
            apply_event(clean)
        except ControlError as exc:
            raise SessionError("control-rejected", str(exc)) from exc
        session.control_seq = clean["seq"]
        return {"ok": True, "seq": clean["seq"]}
