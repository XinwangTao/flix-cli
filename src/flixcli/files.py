"""File transfer over the confirmed wire.

Sender: POST /bubble (type=3, content={state:2, meta:{...resourceId}, ...})
        then POST /file?share_id=<resourceId>&file_name=<name> with the raw
        file body (chunked octet-stream) to the same peer.
Receiver: /file handler writes the body into the save directory, only when a
pending file bubble with that resourceId exists (protocol.md §8).

Both behaviors are verbatim from official captures (capbig.pcapng 2026-10-21).
"""
from __future__ import annotations

import asyncio
import mimetypes
import os
import uuid

import aiohttp

from . import DEVICE_PORT
from .channel import PeerError
from .model import dumps, file_bubble


def guess_mime(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


async def send_file(ip: str, path: str, *, from_fp: str, to_fp: str,
                    port: int = DEVICE_PORT, timeout: float = 600.0) -> dict:
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        return {"ok": False, "error": "not-found", "detail": path}
    size = os.path.getsize(path)
    name = os.path.basename(path)
    # Official uses the SAME uuid for bubble id and meta.resourceId — the
    # receiver looks the bubble up by share_id directly [RT log
    # "_receiveFile shareId=... bubble = null ... should not null"].
    resource_id = str(uuid.uuid4())
    bubble = file_bubble(from_fp, to_fp, resource_id=resource_id, name=name,
                         mime_type=guess_mime(path), size=size)
    bubble["id"] = resource_id
    url = f"http://{ip}:{port}/bubble"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, data=dumps(bubble),
                              headers={"Content-Type": "application/json"},
                              timeout=aiohttp.ClientTimeout(total=30)) as r:
                reply = (await r.text()).strip()
                if r.status != 200:
                    return {"ok": False, "error": "rejected",
                            "detail": f"HTTP {r.status} {reply}"}
            async with aiohttp.ClientSession() as s:
                async with s.post(
                        f"http://{ip}:{port}/file",
                        params={"share_id": resource_id, "file_name": name},
                        data=ChunkedFile(path),
                        headers={"Content-Type": "application/octet-stream"},
                        timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                    if r.status != 200:
                        return {"ok": False, "error": "upload-failed",
                                "detail": f"HTTP {r.status}"}
        return {"ok": True, "id": bubble["id"], "resourceId": resource_id,
                "name": name, "size": size, "to": to_fp}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "timeout"}
    except (aiohttp.ClientError, OSError) as e:
        return {"ok": False, "error": "unreachable", "detail": str(e)[:140]}


class ChunkedFile:
    """async-iterable file reader for aiohttp chunked upload."""

    def __init__(self, path: str, chunk_size: int = 256 * 1024):
        self.path = path
        self.chunk_size = chunk_size

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        f = await asyncio.to_thread(open, self.path, "rb")
        try:
            while True:
                chunk = await asyncio.to_thread(f.read, self.chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            await asyncio.to_thread(f.close)
