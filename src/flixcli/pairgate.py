"""Operator approval for peer-initiated pairing.

Official `askPairDevice` carries the *receiver's* pairing key, which a one-tap
phone flow cannot know. Instead of rejecting such a request, hold it until the
desktop operator approves (GUI dialog) or rejects it (timeout / explicit no).
The HTTP reply is delayed, so the phone still sees one success/reject.
"""
from __future__ import annotations

import asyncio


class PairGate:
    def __init__(self, timeout: float = 30.0, max_pending: int = 8):
        self.timeout = timeout
        self.max_pending = max_pending
        self._pending: dict[str, asyncio.Future] = {}
        self._info: dict[str, dict] = {}

    async def ask(self, fingerprint: str, info: dict) -> bool:
        """Wait for the operator. Returns True only on explicit approval."""
        if not fingerprint or fingerprint in self._pending:
            return False
        if len(self._pending) >= self.max_pending:
            return False
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[fingerprint] = future
        self._info[fingerprint] = dict(info)
        try:
            return await asyncio.wait_for(future, timeout=self.timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            self._pending.pop(fingerprint, None)
            self._info.pop(fingerprint, None)

    def approve(self, fingerprint: str, accept: bool = True) -> bool:
        future = self._pending.get(fingerprint)
        if future is None or future.done():
            return False
        future.set_result(bool(accept))
        return True

    def pending(self) -> list[dict]:
        return [{"fingerprint": fp, **info} for fp, info in self._info.items()]

    def is_pending(self, fingerprint: str) -> bool:
        return fingerprint in self._pending


GATE = PairGate()
