"""Local clipboard access shared by flixd / ``flix serve``.

Windows uses user32 directly for both directions: ``CF_UNICODETEXT`` to read
and ``SetClipboardData`` to write.  Round-tripping through ``clip.exe`` looked
equivalent but is not: the child converts stdin from the *console code page*,
so a UTF-16LE payload (or UTF-8, or GBK) carrying Chinese came back as
``'﻿??????'`` on the receiving machine even though the board logged the
full text.  Going through user32 keeps the text UTF-16 end to end, spawns no
extra process and cannot flash a console window.

Linux desktops go through the usual Wayland/X11 helpers when a session is
present; macOS uses ``pbpaste``/``pbcopy``.

A headless Linux box (the QCM6490 board runs flixd under systemd without
X/Wayland) has no clipboard at all.  Returning ``None`` there made every
incoming clipboard intent a silent success that evaporated: the sender got
``{0: success}`` and the receiver kept nothing.  When no helper exists we
spool the text to a 0600 file next to the database so the payload is still
observable (``cat ~/flixcli/clipboard.txt``) and can be picked up by a local
agent.  The spool is intentionally write-only: re-reading it would make a
headless node broadcast stale text on the next daemon start.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

# Windows gives every console child (clip.exe) a brand-new visible terminal
# unless suppressed; 0 on non-Windows keeps Popen happy there.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_SPOOL_LOCK = threading.Lock()

# Linux/macOS helpers in preference order.  (read, write) argument vectors.
_POSIX_HELPERS = {
    "wl-clipboard": (("wl-paste", "--no-newline"), ("wl-copy",)),
    "xclip": (("xclip", "-selection", "clipboard", "-o"),
              ("xclip", "-selection", "clipboard", "-i")),
    "xsel": (("xsel", "--clipboard", "--output"),
             ("xsel", "--clipboard", "--input")),
    "pbcopy": (("pbpaste",), ("pbcopy",)),
}


def backend() -> str:
    """Name of the clipboard this process can talk to; "" when headless.

    Session variables are checked on purpose: ``xclip`` may be installed on a
    board that only boots to a serial console, and calling it there blocks for
    seconds before failing.  Windows/macOS do not need the env check.
    """
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "pbcopy" if shutil.which("pbpaste") and shutil.which("pbcopy") else ""
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste") \
            and shutil.which("wl-copy"):
        return "wl-clipboard"
    if os.environ.get("DISPLAY"):
        if shutil.which("xclip"):
            return "xclip"
        if shutil.which("xsel"):
            return "xsel"
    return ""


def _run(argv: tuple[str, ...], data: bytes | None = None,
         timeout: float = 2.0) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, input=data, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=timeout,
                              creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None


def _open_clipboard(u32, tries: int = 25, delay: float = 0.02) -> bool:
    """OpenClipboard with a short retry window.

    Clipboard managers, Office and our own clip-sync poller hold the clipboard
    for milliseconds at a time; a single false from OpenClipboard used to drop
    an incoming sync to the spool (or make clip-sync read ``None``) at random.
    """
    import time
    for i in range(tries):
        if u32.OpenClipboard(None):
            return True
        if i + 1 < tries:
            time.sleep(delay)
    return False


def _read_windows() -> str | None:
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    CF_UNICODETEXT = 13
    # Handles/pointers come back as 64-bit values; without explicit restypes
    # ctypes truncates them to 32-bit c_int and reads garbage -- observed as
    # random access violations in wstring_at (0x7F591B90-style addresses).
    u32.GetClipboardData.argtypes = [wintypes.UINT]
    u32.GetClipboardData.restype = wintypes.HANDLE
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalLock.restype = wintypes.LPVOID
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    if not _open_clipboard(u32):
        return None
    try:
        h = u32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = k32.GlobalLock(h)
        if not p:
            return None
        try:
            text = ctypes.wstring_at(p)
        finally:
            k32.GlobalUnlock(h)
    finally:
        u32.CloseClipboard()
    # A leading U+FEFF is a leftover from whoever wrote the clipboard through
    # clip.exe; it is invisible in the UI and must not be broadcast as content.
    return text[1:] if text.startswith("﻿") else text


def _write_windows(text: str) -> bool:
    """Set the clipboard to *text* through user32 (no child process).

    ``clip.exe`` is deliberately not used: it re-encodes stdin via the console
    code page, which turned Chinese into ``????`` even for a correct UTF-16LE
    payload. Returns False when the clipboard is unavailable, so the caller can
    fall back to spooling the text instead of reporting a bogus success.
    """
    import ctypes
    from ctypes import wintypes

    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.OpenClipboard.restype = wintypes.BOOL
    u32.EmptyClipboard.restype = wintypes.BOOL
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u32.SetClipboardData.restype = wintypes.HANDLE
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalLock.restype = wintypes.LPVOID
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalFree.argtypes = [wintypes.HGLOBAL]

    data = text.encode("utf-16-le") + b"\x00\x00"
    handle = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        return False
    ptr = k32.GlobalLock(handle)
    if not ptr:
        k32.GlobalFree(handle)
        return False
    ctypes.memmove(ptr, data, len(data))
    k32.GlobalUnlock(handle)

    # Another process (clipboard managers, Office) can hold the clipboard for
    # a few milliseconds; retrying beats dropping an incoming sync.
    if not _open_clipboard(u32):
        k32.GlobalFree(handle)
        return False
    try:
        if not u32.EmptyClipboard():
            k32.GlobalFree(handle)
            return False
        if not u32.SetClipboardData(CF_UNICODETEXT, handle):
            k32.GlobalFree(handle)
            return False
        # Ownership passed to the system on success; do not free *handle*.
        return True
    finally:
        u32.CloseClipboard()


def read_text() -> str | None:
    """Local clipboard text, or None when empty/unavailable."""
    name = backend()
    if name == "win32":
        return _read_windows()
    if not name:
        return None
    argv = _POSIX_HELPERS[name][0]
    proc = _run(argv, timeout=1.5 if name != "pbcopy" else 5.0)
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def describe(spool: Path | str | None = None) -> str:
    """Health-probe label: where an incoming clipboard would land."""
    name = backend()
    if name:
        return name
    return "file" if spool is not None else "none"


def _spool_write(path: Path, text: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with _SPOOL_LOCK:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            # os.open's mode only applies when the file is created (POSIX);
            # chmod keeps a pre-existing spool private too. Windows maps this
            # to a read-only bit, harmless for our own file.
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, path)
        return True
    except OSError:
        return False


def write_text(text: str, spool: Path | str | None = None) -> str:
    """Write *text* to the local clipboard; return the sink that accepted it.

    Sinks: ``win32``, ``wl-clipboard``, ``xclip``, ``xsel``, ``pbcopy``,
    ``file:<path>`` (headless fallback) or "" when nothing worked.
    """
    name = backend()
    if name == "win32" and _write_windows(text):
        return "win32"
    if name and name != "win32":
        argv = _POSIX_HELPERS[name][1]
        proc = _run(argv, text.encode("utf-8"),
                    timeout=6.0 if name == "pbcopy" else 3.0)
        if proc is not None and proc.returncode == 0:
            return name
    # No GUI clipboard (headless board) or the helper failed: keep the payload
    # observable instead of dropping it.
    if spool is not None and _spool_write(Path(spool), text):
        return "file:%s" % spool
    return ""


async def awrite_text(text: str, spool: Path | str | None = None) -> str:
    return await asyncio.to_thread(write_text, text, spool)
