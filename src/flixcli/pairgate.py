"""Operator approval for peer-initiated pairing.

Official `askPairDevice` carries the *receiver's* pairing key, which a one-tap
phone flow cannot know. Instead of rejecting such a request, hold it until the
desktop operator approves. Two callers use the gate differently:

* The HTTP ask waits briefly (fast path). A prompt that is answered in time
  returns success directly; otherwise the ask returns immediately and the
  approval is delivered later as `confirmPairDevice`, matching the official
  asynchronous flow and staying below the peer's 8 s HTTP timeout.
* First-contact messages register a request without blocking the sender.
"""
from __future__ import annotations

import asyncio


class PairGate:
    def __init__(self, timeout: float = 120.0, max_pending: int = 8,
                 fast_timeout: float = 6.0):
        self.timeout = timeout
        self.fast_timeout = fast_timeout
        self.max_pending = max_pending
        self._pending: dict[str, asyncio.Future] = {}
        self._info: dict[str, dict] = {}

    def open(self, fingerprint: str, info: dict) -> bool:
        """Register a request without waiting. False if duplicate/over capacity."""
        if not fingerprint or fingerprint in self._pending:
            return False
        if len(self._pending) >= self.max_pending:
            return False
        self._pending[fingerprint] = asyncio.get_running_loop().create_future()
        self._info[fingerprint] = dict(info)
        return True

    async def wait(self, fingerprint: str, timeout: float | None) -> bool | None:
        """Wait for a decision. None means "still pending" (timeout)."""
        future = self._pending.get(fingerprint)
        if future is None:
            return False
        if timeout is None:
            return await future
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except asyncio.TimeoutError:
            return None

    def approve(self, fingerprint: str, accept: bool = True) -> bool:
        future = self._pending.get(fingerprint)
        if future is None or future.done():
            return False
        future.set_result(bool(accept))
        return True

    def close(self, fingerprint: str) -> None:
        self._pending.pop(fingerprint, None)
        self._info.pop(fingerprint, None)

    async def ask(self, fingerprint: str, info: dict) -> bool:
        """Blocking variant for callers that do not need a fast HTTP reply."""
        if not self.open(fingerprint, info):
            return False
        try:
            result = await self.wait(fingerprint, self.timeout)
            return bool(result)
        finally:
            self.close(fingerprint)

    def pending(self) -> list[dict]:
        return [{"fingerprint": fp, **info} for fp, info in self._info.items()]

    def is_pending(self, fingerprint: str) -> bool:
        return fingerprint in self._pending


GATE = PairGate()
