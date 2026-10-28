"""Safe local Android Debug Bridge adapter.

ADB is a local operator tool, not a remote command tunnel. Commands are always
passed as an argv list. Shell execution requires an explicit approval flag so
an AI caller cannot silently turn a device connection into arbitrary execution.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path


class AdbError(RuntimeError):
    def __init__(self, kind: str, detail: str, *, returncode: int | None = None):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.returncode = returncode


@dataclass(frozen=True)
class AdbDevice:
    serial: str
    state: str
    model: str = ""
    product: str = ""
    transport: str = ""

    def as_dict(self) -> dict:
        return {"serial": self.serial, "state": self.state, "model": self.model,
                "product": self.product, "transport": self.transport,
                "authorized": self.state == "device"}


class AdbClient:
    _ANDROID_ROOTS = {"sdcard", "data", "storage", "system", "vendor", "product"}

    def __init__(self, adb_path: str | None = None, timeout: float = 30.0):
        self.adb_path = adb_path or os.environ.get("ANDROID_ADB", "adb")
        self.timeout = timeout

    @classmethod
    def _restore_msys_remote_path(cls, value: str) -> str:
        """Undo Git Bash's pre-process conversion of Android absolute paths."""
        if os.name != "nt" or not os.environ.get("MSYSTEM"):
            return value
        shell = os.environ.get("SHELL", "").replace("/", "\\")
        suffix = "\\bin\\bash.exe"
        if not shell.lower().endswith(suffix):
            return value
        root = shell[:-len(suffix)].replace("\\", "/").rstrip("/") + "/"
        normalized = value.replace("\\", "/")
        if not normalized.lower().startswith(root.lower()):
            return value
        tail = normalized[len(root):].lstrip("/")
        first = tail.partition("/")[0].lower()
        return "/" + tail if first in cls._ANDROID_ROOTS else value

    def available(self) -> bool:
        return bool(shutil.which(self.adb_path) or Path(self.adb_path).exists())

    async def _run(self, *args: str, timeout: float | None = None) -> tuple[int, str, str]:
        if not self.available():
            raise AdbError("adb-not-found", f"adb executable not found: {self.adb_path}")
        try:
            env = None
            if os.name == "nt":
                # Git Bash/MSYS rewrites POSIX-looking remote Android paths
                # before adb sees them. Keep /sdcard/... literal.
                env = {**os.environ, "MSYS_NO_PATHCONV": "1",
                       "MSYS2_ARG_CONV_EXCL": "*"}
            proc = await asyncio.create_subprocess_exec(
                self.adb_path, *args, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env)
            out, err = await asyncio.wait_for(
                proc.communicate(), timeout=timeout or self.timeout)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise AdbError("timeout", f"adb timed out: {' '.join(args)}") from exc
        except OSError as exc:
            raise AdbError("adb-error", str(exc)) from exc
        return proc.returncode, out.decode("utf-8", "replace").strip(), \
            err.decode("utf-8", "replace").strip()

    async def devices(self) -> list[AdbDevice]:
        code, out, err = await self._run("devices", "-l", timeout=10)
        if code != 0:
            raise AdbError("adb-error", err or out, returncode=code)
        rows = []
        for line in out.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 2 or fields[0].startswith("*"):
                continue
            attrs = {}
            for field in fields[2:]:
                if ":" in field:
                    key, value = field.split(":", 1)
                    attrs[key] = value
            rows.append(AdbDevice(fields[0], fields[1], attrs.get("model", ""),
                                   attrs.get("product", ""), attrs.get("transport_id", "")))
        return rows

    def _require_serial(self, serial: str | None, devices: list[AdbDevice]) -> str:
        if serial:
            if not any(d.serial == serial for d in devices):
                raise AdbError("unknown-device", f"ADB serial not found: {serial}")
            return serial
        ready = [d for d in devices if d.state == "device"]
        if len(ready) != 1:
            raise AdbError("ambiguous-device", "specify --serial when zero or multiple authorized devices exist")
        return ready[0].serial

    async def push(self, source: str, destination: str, serial: str | None = None) -> dict:
        devices = await self.devices()
        target = self._require_serial(serial, devices)
        destination = self._restore_msys_remote_path(destination)
        if not Path(source).is_file():
            raise AdbError("not-found", f"local file not found: {source}")
        started = time.monotonic()
        code, out, err = await self._run("-s", target, "push", source, destination,
                                         timeout=max(self.timeout, 300))
        if code:
            raise AdbError("transfer-failed", err or out, returncode=code)
        return {"ok": True, "operation": "push", "serial": target,
                "source": str(Path(source).resolve()), "destination": destination,
                "output": out, "elapsed": round(time.monotonic() - started, 3)}

    async def pull(self, source: str, destination: str, serial: str | None = None) -> dict:
        devices = await self.devices()
        target = self._require_serial(serial, devices)
        source = self._restore_msys_remote_path(source)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        code, out, err = await self._run("-s", target, "pull", source, destination,
                                         timeout=max(self.timeout, 300))
        if code:
            raise AdbError("transfer-failed", err or out, returncode=code)
        return {"ok": True, "operation": "pull", "serial": target,
                "source": source, "destination": str(Path(destination).resolve()),
                "output": out, "elapsed": round(time.monotonic() - started, 3)}

    async def shell(self, command: list[str], serial: str | None = None,
                    approved: bool = False) -> dict:
        if not approved:
            return {"ok": False, "error": "approval_required", "approved": False,
                    "command": command, "detail": "ADB shell requires explicit approval"}
        if not command or any("\x00" in part for part in command):
            raise AdbError("bad-command", "command must contain non-empty NUL-free arguments")
        devices = await self.devices()
        target = self._require_serial(serial, devices)
        started = time.monotonic()
        code, out, err = await self._run("-s", target, "shell", *command,
                                         timeout=max(self.timeout, 120))
        return {"ok": code == 0, "serial": target, "command": command,
                "stdout": out, "stderr": err, "returncode": code,
                "elapsed": round(time.monotonic() - started, 3)}

    async def report(self, serial: str | None = None) -> dict:
        devices = await self.devices()
        selected = self._require_serial(serial, devices) if serial else None
        return {"ok": True, "adb": self.adb_path, "devices": [d.as_dict() for d in devices],
                "selected": selected}
