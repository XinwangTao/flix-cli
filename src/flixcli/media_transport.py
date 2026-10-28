"""Optional aiortc transport for approved flixcli LAN sessions.

Importing this module does not require media dependencies. `available()` reports
whether the `media` extra is installed. Screen capture is primary-monitor only,
12 fps, scaled to at most 1280 px on the longest edge. No STUN/TURN servers are
configured, so only host ICE candidates are used.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import time
from fractions import Fraction
from pathlib import Path
from typing import Awaitable, Callable

from .control import ControlError
from .media_session import SessionError, SessionManager

try:
    import av
    import mss
    import numpy as np
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
    MEDIA_IMPORT_ERROR = None
except ImportError as exc:  # optional dependency
    av = mss = np = RTCConfiguration = RTCPeerConnection = RTCSessionDescription = VideoStreamTrack = None
    MEDIA_IMPORT_ERROR = str(exc)


def available() -> bool:
    return MEDIA_IMPORT_ERROR is None


class ScreenTrack(VideoStreamTrack if VideoStreamTrack is not None else object):
    def __init__(self, fps: int = 12, max_edge: int = 1280):
        if not available():
            raise RuntimeError(MEDIA_IMPORT_ERROR)
        super().__init__()
        self.fps = fps
        self.max_edge = max_edge
        self._started = time.monotonic()
        self._frame = 0
        with mss.MSS() as grabber:
            self._monitor = dict(grabber.monitors[1])

    async def recv(self):
        target = self._started + self._frame / self.fps
        await asyncio.sleep(max(0, target - time.monotonic()))
        self._frame += 1
        def grab():
            with mss.MSS() as grabber:
                return grabber.grab(self._monitor)
        image = await asyncio.to_thread(grab)
        frame = av.VideoFrame.from_ndarray(np.asarray(image), format="bgra")
        longest = max(frame.width, frame.height)
        if longest > self.max_edge:
            scale = self.max_edge / longest
            frame = frame.reformat(width=round(frame.width * scale),
                                   height=round(frame.height * scale), format="yuv420p")
        else:
            frame = frame.reformat(format="yuv420p")
        frame.pts = self._frame
        frame.time_base = Fraction(1, self.fps)
        return frame

    def stop(self):
        super().stop()


class PeerMedia:
    def __init__(self, call_id: str, manager: SessionManager,
                 frame_callback: Callable[[str, bytes], Awaitable[None]] | None = None):
        if not available():
            raise SessionError("media-unavailable", MEDIA_IMPORT_ERROR or "media extra missing")
        self.call_id = call_id
        self.manager = manager
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.frame_callback = frame_callback
        self.screen_track = None
        self.receive_task: asyncio.Task | None = None
        self.signal_seq = 0
        self.control_channel = None
        self._wire_events()

    def _wire_events(self):
        @self.pc.on("connectionstatechange")
        async def on_state():
            session = self.manager.require(self.call_id)
            state = self.pc.connectionState
            if state == "connected":
                session.state = "active"
                self.manager._emit(session, "media-connected")
            elif state in ("failed", "closed", "disconnected"):
                session.state = "closed" if state == "closed" else "failed"
                session.reason = f"media {state}"
                self.manager._emit(session, f"media-{state}")

        @self.pc.on("track")
        def on_track(track):
            if track.kind == "video":
                self.receive_task = asyncio.create_task(self._receive_video(track))

        @self.pc.on("datachannel")
        def on_channel(channel):
            if channel.label != "flix-control-v1":
                channel.close()
                return

            @channel.on("message")
            def on_message(message):
                if not isinstance(message, str) or len(message) > 2048:
                    return
                try:
                    self.manager.handle_control(self.call_id, json.loads(message))
                except (ValueError, ControlError, SessionError, json.JSONDecodeError):
                    return

    async def _receive_video(self, track):
        while True:
            try:
                frame = await track.recv()
            except Exception:
                return
            if self.frame_callback is None:
                continue
            image = frame.to_image()
            buf = io.BytesIO()
            image.save(buf, format="JPEG", quality=75)
            await self.frame_callback(self.call_id, buf.getvalue())

    def add_screen(self):
        self.screen_track = ScreenTrack()
        self.pc.addTrack(self.screen_track)

    def add_control_channel(self):
        self.control_channel = self.pc.createDataChannel("flix-control-v1", ordered=True)
        return self.control_channel

    def send_control(self, event: dict) -> None:
        if self.control_channel is None or self.control_channel.readyState != "open":
            raise SessionError("control-unavailable", "control data channel is not open")
        self.control_channel.send(json.dumps(event, separators=(",", ":")))

    async def offer(self) -> dict:
        self.pc.addTransceiver("video", direction="recvonly")
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        session = self.manager.require(self.call_id)
        session.state = "connecting"
        self.manager._emit(session, "offer-created")
        return {"type": self.pc.localDescription.type, "sdp": self.pc.localDescription.sdp}

    async def answer(self, offer: dict, share_screen: bool = True) -> dict:
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=offer["sdp"],
                                                                 type=offer["type"]))
        if share_screen:
            self.add_screen()
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        session = self.manager.require(self.call_id)
        session.state = "connecting"
        self.manager._emit(session, "answer-created")
        return {"type": self.pc.localDescription.type, "sdp": self.pc.localDescription.sdp}

    async def apply_answer(self, answer: dict) -> None:
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"],
                                                                 type=answer["type"]))

    async def close(self):
        if self.receive_task:
            self.receive_task.cancel()
        if self.screen_track:
            self.screen_track.stop()
        await self.pc.close()


class MediaRuntime:
    def __init__(self, manager: SessionManager, snapshot_dir: Path):
        self.manager = manager
        self.snapshot_dir = snapshot_dir
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.peers: dict[str, PeerMedia] = {}

    async def _save_frame(self, call_id: str, data: bytes) -> None:
        path = self.snapshot_dir / f"{call_id}.jpg"
        tmp = path.with_suffix(".jpg.tmp")
        await asyncio.to_thread(tmp.write_bytes, data)
        await asyncio.to_thread(tmp.replace, path)

    def require_available(self) -> None:
        if not available():
            raise SessionError("media-unavailable",
                               "install the media extra: pip install -e .[media]")

    def create(self, call_id: str) -> PeerMedia:
        self.require_available()
        peer = PeerMedia(call_id, self.manager, self._save_frame)
        self.peers[call_id] = peer
        self.manager.media[call_id] = peer
        return peer

    def get(self, call_id: str) -> PeerMedia:
        peer = self.peers.get(call_id)
        if peer is None:
            raise SessionError("no-media", "media transport not created")
        return peer

    def snapshot(self, call_id: str) -> Path | None:
        path = self.snapshot_dir / f"{call_id}.jpg"
        return path if path.is_file() else None
