"""Peer device channel client (HTTP on TCP 8891).

Confirmed routes on the official client: /intent (POST, TransIntent JSON),
/bubble (POST, bubble JSON -> 200 "received bubble"), /upload, /download/<id>,
/file. Response/error strings are part of the protocol: 404 "Route not found",
/intent parse failure 500 "receive intent failed".
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp

from . import DEVICE_PORT, model
from .model import dumps


class PeerError(Exception):
    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


async def post_bubble(ip: str, bubble: dict, port: int = DEVICE_PORT,
                      timeout: float = 8.0) -> str:
    """Send a bubble; returns the response text ('received bubble')."""
    url = f"http://{ip}:{port}/bubble"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, data=dumps(bubble),
                              headers={"Content-Type": "application/json"},
                              timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                text = (await r.text()).strip()
                if r.status != 200:
                    raise PeerError("rejected", f"HTTP {r.status} {text}")
                return text
    except asyncio.TimeoutError:
        raise PeerError("timeout", url)
    except aiohttp.ClientError as e:
        raise PeerError("unreachable", f"{url}: {e}")


async def post_intent(ip: str, intent: dict, port: int = DEVICE_PORT,
                      timeout: float = 8.0) -> str:
    """Send a TransIntent; official success body is the literal `{0: success}`."""
    url = f"http://{ip}:{port}/intent"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, data=dumps(intent),
                              headers={"Content-Type": "application/json"},
                              timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                text = await r.text()
                if r.status != 200:
                    raise PeerError("rejected", f"HTTP {r.status} {text.strip()}")
                return text.strip()
    except asyncio.TimeoutError:
        raise PeerError("timeout", url)
    except aiohttp.ClientError as e:
        raise PeerError("unreachable", f"{url}: {e}")


async def get_json(url: str, timeout: float = 5.0) -> Any:
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            return json.loads(await r.text())


async def ping_v2(ip: str, ident: dict, port: int = DEVICE_PORT,
                  device_port: int = DEVICE_PORT, timeout: float = 6.0,
                  from_: str = "manual") -> dict:
    """Manual-add handshake: POST /ping_v2, return the peer's deviceModal.

    Official semantics [RT 2026-10-21]: the peer answers 200 text/plain with
    its own deviceModal double-encoded; an invalid body gets 400 "Bad Request".
    """
    url = f"http://{ip}:{port}/ping_v2"
    body = dumps(model.ping_v2_body(ident, device_port, from_=from_))
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, data=body,
                              headers={"Content-Type":
                                       "application/json; charset=UTF-8"},
                              timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                text = await r.text()
                if r.status == 400:
                    raise PeerError("bad_request", text.strip())
                if r.status != 200:
                    raise PeerError("rejected", f"HTTP {r.status} {text.strip()}")
                return model.parse_ping_v2_response(text)
    except asyncio.TimeoutError:
        raise PeerError("timeout", url)
    except aiohttp.ClientError as e:
        raise PeerError("unreachable", f"{url}: {e}")


async def pair_device(ip: str, my_fp: str, peer_fp: str, peer_code: str,
                      port: int = DEVICE_PORT, timeout: float = 6.0,
                      action: str = model.ACTION_ASK_PAIR) -> tuple[int, str]:
    """Pairing handshake over /intent. Returns (code, message); 0 = success.

    Official replies are Dart map toString (`{0: success}`,
    `{301: pair Device Null}`, `{302: device not match ...}`).
    """
    url = f"http://{ip}:{port}/intent"
    body = dumps(model.pair_intent(my_fp, peer_fp, peer_code, action=action))
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, data=body,
                              headers={"Content-Type": "application/json"},
                              timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                text = await r.text()
                if r.status != 200:
                    raise PeerError("rejected", f"HTTP {r.status} {text.strip()}")
                return model.parse_intent_reply(text)
    except asyncio.TimeoutError:
        raise PeerError("timeout", url)
    except aiohttp.ClientError as e:
        raise PeerError("unreachable", f"{url}: {e}")


async def post_json(ip: str, path: str, payload: dict, port: int = DEVICE_PORT,
                    timeout: float = 6.0) -> dict:
    """Generic JSON POST to a device channel endpoint; returns parsed JSON."""
    url = f"http://{ip}:{port}{path}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, data=dumps(payload),
                              headers={"Content-Type": "application/json"},
                              timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                text = await r.text()
                if r.status != 200:
                    raise PeerError("rejected", f"HTTP {r.status} {text.strip()}")
                try:
                    return json.loads(text)
                except ValueError:
                    return {"raw": text}
    except asyncio.TimeoutError:
        raise PeerError("timeout", url)
    except aiohttp.ClientError as e:
        raise PeerError("unreachable", f"{url}: {e}")
