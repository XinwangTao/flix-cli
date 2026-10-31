"""Remote command execution over an already-paired device channel (rdShell).

Phase 1 is exec-only: no PTY, no streaming, no interactive programs. The
target runs the command as the flixd user and returns stdout/stderr plus an
exit code. Output is capped so a runaway command cannot fill the peer's
memory, and a timeout kills the whole process tree (a shell that spawns a
child must not leave it running).
"""
from __future__ import annotations

import os
import subprocess
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
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = _NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, **kwargs)
    timed_out = False
    try:
        out, err = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        out, err = proc.communicate()
    stdout, cut_out = _clip(out or b"")
    stderr, cut_err = _clip(err or b"")
    return {
        "exit": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": cut_out or cut_err,
        "timeout": timed_out,
        "durationMs": int((time.monotonic() - started) * 1000),
    }
