"""Pairing QR payload generator.

Official clients encode https://flix.center/qrcode/pair/<payload> [IDA pool];
the exact payload codec is still undecoded (protocol.md §11), so flixcli uses
its own verifiable format: base64url(json {"ips": [...], "port": N,
"code": "<4-digit key>", "fp": fingerprint, "v": 1}).

`flix qrcode` prints the payload text; any QR encoder (e.g.
`qrencode -t ANSI` or a phone) can render it. Scanning side:
`flix pair --code <key> <ip:port>`.
"""
from __future__ import annotations

import base64
import json
import socket

from . import local_ip


def pair_payload(fingerprint: str, port: int, code: str,
                 ips: list[str] | None = None) -> str:
    ips = ips or [local_ip()]
    doc = {"ips": ips, "port": int(port), "code": str(code),
           "fp": fingerprint, "v": 1}
    raw = json.dumps(doc, separators=(",", ":")).encode()
    return "flix2://" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def parse_payload(text: str) -> dict | None:
    """Decode a flix2:// payload (or bare base64url) back to its fields."""
    body = text
    if body.startswith("flix2://"):
        body = body[len("flix2://"):]
    try:
        pad = "=" * (-len(body) % 4)
        doc = json.loads(base64.urlsafe_b64decode(body + pad))
        if isinstance(doc, dict) and doc.get("ips") and doc.get("port"):
            return doc
    except Exception:
        return None
    return None


def lan_ips() -> list[str]:
    ips = [local_ip()]
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips
