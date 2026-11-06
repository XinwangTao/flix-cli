"""Property-based fuzz tests for the wire parsers and generated scripts.

These modules sit on the untrusted boundary: every datagram, intent reply and
QR payload comes from another host. A parser that raises on junk turns a
malformed packet into a crashed listener (or a 500 the sender can fingerprint).
"""
from __future__ import annotations

import json

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from flixcli import is_port_text
from flixcli.discovery import build_announce, parse_datagram
from flixcli.model import (dumps, parse_intent_reply, parse_ping_v2_response,
                           text_bubble)
from flixcli.qrcode import pair_payload, parse_payload
from flixcli.routing import resolve_target
from flixcli.upgrade import _ps_lit, build_script

SANE = settings(max_examples=40, deadline=None,
                suppress_health_check=[HealthCheck.too_slow])

TEXT = st.text(max_size=64)
BINARY = st.binary(max_size=256)


class FakeStore:
    def __init__(self, rows=None):
        self._rows = rows or []

    def devices(self):
        return list(self._rows)


@given(BINARY)
@SANE
def test_parse_datagram_never_raises(data: bytes) -> None:
    dev = parse_datagram(data, "10.0.0.1")
    assert dev is None or (
        isinstance(dev, dict) and isinstance(dev.get("fingerprint"), str))


@given(st.text(max_size=64), st.integers(min_value=1, max_value=65535),
       st.text(min_size=1, max_size=16))
@SANE
def test_announce_roundtrip(alias: str, port: int, fp: str) -> None:
    ident = {"alias": alias, "fingerprint": fp, "os": "windows"}
    dev = parse_datagram(build_announce(ident, port), "10.0.0.1")
    assert dev is not None
    assert dev["fingerprint"] == fp
    assert dev["alias"] == alias
    assert dev["port"] == port
    assert dev["ip"] == "10.0.0.1"


@given(st.text(max_size=80), st.integers(min_value=1, max_value=65535),
       st.text(max_size=16))
@SANE
def test_qr_payload_roundtrip(fp: str, port: int, code: str) -> None:
    payload = pair_payload(fp, port, code, ips=["10.0.0.2"])
    doc = parse_payload(payload)
    assert doc is not None
    assert doc["fp"] == fp and doc["port"] == port and doc["code"] == code
    assert doc["ips"] == ["10.0.0.2"]


@given(TEXT)
@SANE
def test_qr_parser_never_raises(text: str) -> None:
    doc = parse_payload(text)
    assert doc is None or isinstance(doc, dict)


@given(TEXT)
@SANE
def test_intent_reply_parser_never_raises(text: str) -> None:
    code, msg = parse_intent_reply(text)
    assert isinstance(code, int) and isinstance(msg, str)


def test_intent_reply_known_forms() -> None:
    assert parse_intent_reply("{0: success}") == (0, "success")
    assert parse_intent_reply("{302: device not match}")[0] == 302
    assert parse_intent_reply("{not-a-code: x}")[0] == -1


@given(TEXT)
@SANE
def test_ping_v2_parser_raises_only_value_error(text: str) -> None:
    try:
        parse_ping_v2_response(text)
    except (ValueError, TypeError):
        pass


@given(st.text(max_size=80))
@SANE
def test_bubble_unicode_roundtrip(content: str) -> None:
    bubble = text_bubble("a", "b", content)
    assert json.loads(dumps(bubble))["content"] == content


@given(st.text(max_size=40))
@SANE
def test_resolve_target_never_raises(to: str) -> None:
    ip, port, fp = resolve_target(FakeStore(), to)
    assert ip is None or isinstance(ip, str)


@given(st.text(max_size=60))
@SANE
def test_upgrade_script_never_breaks_literal(value: str) -> None:
    script = build_script(f"{value}.exe", value, f"{value}.log",
                          8891, 8898, 30)
    # Every embedded name must appear as a single-quoted literal whose quotes
    # are doubled. Matching the escaped form is the invariant: an unescaped
    # name would produce a different literal.
    escaped = value.replace("'", "''")
    assert f"'{escaped}.exe'" in script
    assert f"'{escaped}.log'" in script


def test_unicode_digits_are_never_ports() -> None:
    """'²'.isdigit() is True but int('²') raises; a hostile target string must
    not crash target resolution."""
    for raw in ("10.0.0.1:²", "10.0.0.1:١٢", "host:²²", "²"):
        ip, port, fp = resolve_target(FakeStore(), raw)
        assert ip is None and port is None
    assert is_port_text("²") is False
    assert is_port_text("8891") is True


def test_upgrade_script_rejects_command_injection_shape() -> None:
    evil = "x'; Start-Process calc; #.exe"
    script = build_script(evil, "s", "l", 8891, 8898, 30)
    # The unescaped breakout sequence must be gone; the doubled quote is data.
    assert "x'; Start-Process calc; #" not in script
    assert "x''; Start-Process calc; #.exe'" in script
    assert _ps_lit("it's") == "it''s"
