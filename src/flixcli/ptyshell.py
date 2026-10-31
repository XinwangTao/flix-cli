"""Interactive pseudo-terminal sessions (rdShell phase 2).

Windows goes through ConPTY (pywinpty); POSIX uses the stdlib pty module. The
session is byte-oriented on the wire. pywinpty hands us str decoded as UTF-8,
so Windows shells are started with a UTF-8 primer (``chcp 65001``) and the text
is re-encoded on the way out — otherwise cmd's OEM output turns into U+FFFD.
"""
from __future__ import annotations

import os

MAX_IDLE_S = 30 * 60
MAX_SESSIONS = 1
READ_CHUNK = 8192


class PtyUnavailable(RuntimeError):
    """The host cannot provide a pseudo-terminal."""


def available() -> bool:
    if os.name != "nt":
        return True
    try:
        import winpty  # noqa: F401
    except ImportError:
        return False
    return True


def _windows_argv(shell: str) -> str:
    """Shell command line with the UTF-8 primer already applied."""
    name = (shell or "cmd").strip().lower()
    if name in ("powershell", "pwsh"):
        exe = "powershell.exe" if name == "powershell" else "pwsh.exe"
        return (f'{exe} -NoExit -Command "chcp 65001>nul; '
                f'[Console]::OutputEncoding=[Text.UTF8Encoding]::new()"')
    return "cmd.exe /k chcp 65001>nul"


class PtySession:
    """One pseudo-terminal running a shell; blocking reads, called from a thread."""

    def __init__(self, shell: str = "", cols: int = 80, rows: int = 24):
        self.cols = max(int(cols or 80), 20)
        self.rows = max(int(rows or 24), 5)
        self.shell = shell or ""
        self.closed = False
        self.exit_code = None
        self._proc = None
        self._pid = 0
        self._fd = -1
        if os.name == "nt":
            try:
                from winpty import PtyProcess
            except ImportError as exc:  # pragma: no cover - Windows only
                raise PtyUnavailable("pywinpty not installed") from exc
            self._proc = PtyProcess.spawn(_windows_argv(self.shell),
                                          dimensions=(self.rows, self.cols))
        else:
            self._spawn_posix()

    # -- posix ---------------------------------------------------------------
    def _spawn_posix(self) -> None:
        import fcntl
        import pty
        import struct
        import termios

        exe = self.shell or os.environ.get("SHELL") or "/bin/sh"
        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                os.execvp(exe, [exe])
            finally:
                os._exit(127)
        self._pid, self._fd = pid, fd
        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", self.rows, self.cols, 0, 0))

    # -- io ------------------------------------------------------------------
    def read(self, size: int = READ_CHUNK) -> bytes:
        if self.closed:
            return b""
        if os.name == "nt":
            try:
                text = self._proc.read(size)
            except (EOFError, OSError):
                self.closed = True
                return b""
            return text.encode("utf-8", "replace")
        try:
            return os.read(self._fd, size)
        except OSError:
            self.closed = True
            return b""

    def write(self, data: bytes) -> None:
        if not data or self.closed:
            return
        if os.name == "nt":
            self._proc.write(data.decode("utf-8", "replace"))
            return
        os.write(self._fd, data)

    def resize(self, cols: int, rows: int) -> None:
        self.cols = max(int(cols or self.cols), 20)
        self.rows = max(int(rows or self.rows), 5)
        if os.name == "nt":
            try:
                self._proc.setwinsize(self.rows, self.cols)
            except Exception:  # noqa: BLE001 - resizing is best effort
                pass
            return
        import fcntl
        import struct
        import termios
        try:
            fcntl.ioctl(self._fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", self.rows, self.cols, 0, 0))
        except OSError:
            pass

    def isalive(self) -> bool:
        if self.closed:
            return False
        if os.name == "nt":
            return bool(self._proc.isalive())
        try:
            pid, status = os.waitpid(self._pid, os.WNOHANG)
        except OSError:
            return False
        if pid == 0:
            return True
        self.exit_code = os.waitstatus_to_exitcode(status)
        return False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if os.name == "nt":
            try:
                self._proc.close()
            except Exception:  # noqa: BLE001 - already gone
                pass
            return
        try:
            os.close(self._fd)
        except OSError:
            pass
        try:
            os.kill(self._pid, 9)
        except OSError:
            pass
