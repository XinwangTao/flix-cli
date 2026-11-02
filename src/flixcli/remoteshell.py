"""Remote command execution over an already-paired device channel (rdShell).

Phase 1 is exec-only: no PTY, no streaming, no interactive programs. The
target runs the command as the flixd user and returns stdout/stderr plus an
exit code. Output is capped so a runaway command cannot fill the peer's
memory, and a timeout kills the whole process tree (a shell that spawns a
child must not leave it running).
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import threading
import time

MAX_OUTPUT_BYTES = 256 * 1024
DEFAULT_TIMEOUT_S = 30.0
MAX_TIMEOUT_S = 300.0
# Commands are arbitrary strings from a paired peer; a console window must not
# flash on the target every time one runs (same reason as the GUI's children).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _fallback_encodings() -> tuple[str, ...]:
    """Code pages a redirected Windows console actually writes.

    `cmd` children write the OEM code page (cp936 here, not UTF-8), so a strict
    UTF-8 decode fails on Chinese output; POSIX children write the locale codec.
    """
    encs: list[str] = []
    if os.name == "nt":
        try:
            import ctypes
            encs.append(f"cp{ctypes.windll.kernel32.GetOEMCP()}")
        except Exception:  # noqa: BLE001 - best effort
            pass
        encs.append("gbk")
    else:
        import locale
        encs.append(locale.getpreferredencoding(False))
    seen: set[str] = set()
    out: list[str] = []
    for enc in encs:
        if enc and enc.lower() not in seen:
            seen.add(enc.lower())
            out.append(enc)
    return tuple(out)


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for enc in _fallback_encodings():
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def _clip(data: bytes) -> tuple[str, bool]:
    """Decode the output, cutting it at MAX_OUTPUT_BYTES."""
    if len(data) <= MAX_OUTPUT_BYTES:
        return _decode(data), False
    return _decode(data[:MAX_OUTPUT_BYTES]), True


# A peer can ask for several rdShell calls at once. Each occupies a worker
# thread; without a cap, ~32 requests starve the default executor (PTY reads,
# media, everything else).
_MAX_CONCURRENT = 4
_SLOTS = threading.BoundedSemaphore(_MAX_CONCURRENT)


def _drain(pipe, cap: int) -> bytes:
    """Read a pipe to EOF but keep at most `cap` bytes in memory.

    Reading after the child exits is too late: `yes` or a chatty build can
    fill gigabytes into the OS/our pipe before communicate() returns. Draining
    concurrently bounds memory *and* keeps the child from blocking on a full
    pipe.
    """
    buf = bytearray()
    try:
        while True:
            chunk = pipe.read(65536)
            if not chunk:
                break
            room = cap - len(buf)
            if room > 0:
                buf.extend(chunk[:room])
    except (OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass
    return bytes(buf)


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        with subprocess.Popen(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              creationflags=_NO_WINDOW):
            pass
        return
    try:
        os.killpg(os.getpgid(proc.pid), 9)
    except OSError:
        proc.kill()


def run_command(command: str, timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """Run `command` through the platform shell; never raises for exit codes."""
    limit = max(0.1, min(float(timeout or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))
    started = time.monotonic()
    if not _SLOTS.acquire(timeout=2):
        return {"exit": 124, "stdout": "", "stderr": "too many concurrent "
                "remote commands", "truncated": False, "timeout": True,
                "durationMs": int((time.monotonic() - started) * 1000)}
    try:
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = _NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, **kwargs)
        chunks: dict[str, bytes] = {}
        readers = []
        for key, pipe in (("out", proc.stdout), ("err", proc.stderr)):
            t = threading.Thread(target=lambda k=key, p=pipe: chunks.__setitem__(
                k, _drain(p, MAX_OUTPUT_BYTES + 1)), daemon=True)
            t.start()
            readers.append(t)
        timed_out = False
        try:
            proc.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            # The old code called communicate() with no timeout here: a
            # grandchild that kept the pipe open hung the worker forever.
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                proc.wait(timeout=5)
        for t in readers:
            t.join(timeout=5)
        stdout, cut_out = _clip(chunks.get("out", b""))
        stderr, cut_err = _clip(chunks.get("err", b""))
        return {
            "exit": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": cut_out or cut_err,
            "timeout": timed_out,
            "durationMs": int((time.monotonic() - started) * 1000),
        }
    finally:
        _SLOTS.release()
