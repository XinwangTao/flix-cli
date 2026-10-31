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


def _clip(data: bytes) -> tuple[str, bool]:
    """Decode UTF-8 output, cutting it at MAX_OUTPUT_BYTES."""
    if len(data) <= MAX_OUTPUT_BYTES:
        return data.decode("utf-8", "replace"), False
    return data[:MAX_OUTPUT_BYTES].decode("utf-8", "replace"), True


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
