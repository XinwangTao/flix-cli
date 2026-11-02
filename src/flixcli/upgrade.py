"""Remote exe upgrade with a health check and an automatic rollback.

The target may run any older build, so the orchestration lives here: the new
exe goes over the file channel and a fixed PowerShell template goes over
rdShell. The template renames the running exe (Windows refuses to overwrite a
running exe but allows a rename), clears PyInstaller's extraction-dir variable
(inheriting it makes the new build silently reuse the OLD unpacked files), and
restores the backup when the new daemon never binds its port.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tempfile
import time

import aiohttp

from . import DEVICE_PORT
from .channel import PeerError, post_json
from .files import send_file
from .model import ACTION_SHELL, shell_intent
from .routing import resolve_target

LOG_NAME = "rd_upgrade.log"  # 兼容旧脚本；每次升级用独立文件名
SCRIPT_TEMPLATE = r"""$ErrorActionPreference = 'Continue'
$env:FLIX_UPGRADE_FRESH_ENV = '__FRESH__'
$log = Join-Path $env:TEMP '__LOG__'
function Note($m) { Add-Content -Path $log -Value ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $m) -Encoding UTF8 }
$src = Join-Path $env:USERPROFILE 'Downloads\__SRC__'
$dir = Join-Path $env:USERPROFILE 'Downloads\RelayDesk-v2.2.1-win64'
if (-not (Test-Path (Join-Path $dir 'RelayDesk.exe'))) { $dir = Join-Path $env:USERPROFILE 'Downloads' }
$dst = Join-Path $dir 'RelayDesk.exe'
$backup = $dst + '.prev-__STAMP__'
Note ('start src=' + $src + ' dst=' + $dst)
if (-not (Test-Path $src)) { Note 'FAIL missing-src'; exit 1 }

$running = Get-CimInstance Win32_Process -Filter "Name='RelayDesk.exe'" |
    Where-Object { $_.CommandLine -match 'serve' } | Select-Object -First 1
$argLine = 'serve'
if ($running) {
    $cmd = [string]$running.CommandLine
    $at = $cmd.IndexOf('.exe')
    if ($at -ge 0) { $argLine = $cmd.Substring($at + 4).Trim() }
}
if (-not $argLine) { $argLine = 'serve --device-port __PORT__ --api-port __API__' }
Note ('args=' + $argLine)

try {
    Rename-Item -LiteralPath $dst -NewName (Split-Path $backup -Leaf) -Force -ErrorAction Stop
    Note ('backup=' + $backup)
} catch {
    Note ('FAIL rename ' + $_.Exception.Message); exit 1
}
try {
    Copy-Item -LiteralPath $src -Destination $dst -Force -ErrorAction Stop
    Note 'copied'
} catch {
    Note ('FAIL copy ' + $_.Exception.Message)
    Rename-Item -LiteralPath $backup -NewName 'RelayDesk.exe' -Force -ErrorAction SilentlyContinue
    exit 1
}

function Stop-Daemon {
    Get-CimInstance Win32_Process -Filter "Name='RelayDesk.exe'" |
        Where-Object { $_.CommandLine -match 'serve' } |
        ForEach-Object { Note ('stop ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 3
}
function Start-Daemon {
    if ($env:FLIX_UPGRADE_FRESH_ENV -eq '1') {
        Remove-Item Env:_PYI_APPLICATION_HOME_DIR -ErrorAction SilentlyContinue
        Remove-Item Env:_MEIPASS2 -ErrorAction SilentlyContinue
    }
    Start-Process -FilePath $dst -ArgumentList $argLine -WindowStyle Hidden
}

Stop-Daemon
Start-Daemon
$ok = $false
for ($i = 0; $i -lt __TRIES__; $i++) {
    Start-Sleep -Seconds 2
    if ((Get-NetTCPConnection -LocalPort __PORT__ -State Listen -ErrorAction SilentlyContinue | Measure-Object).Count -ge 1) { $ok = $true; break }
}
if ($ok) { Note 'healthy'; exit 0 }

Note 'unhealthy-rollback'
Stop-Daemon
Copy-Item -LiteralPath $backup -Destination $dst -Force
Start-Daemon
Start-Sleep -Seconds 10
$n = (Get-NetTCPConnection -LocalPort __PORT__ -State Listen -ErrorAction SilentlyContinue | Measure-Object).Count
Note ('rollback-listening=' + $n)
if ($n -ge 1) { exit 2 } else { exit 3 }
"""


def verify_package(path: str) -> dict:
    """Local gate: only a readable .exe, with a hash the reply can quote."""
    if not path.lower().endswith(".exe"):
        raise ValueError("only .exe packages are accepted")
    if not os.path.isfile(path):
        raise ValueError(f"not found: {path}")
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return {"path": os.path.abspath(path), "name": os.path.basename(path),
            "size": size, "sha256": digest.hexdigest().upper()}


def build_script(src_name: str, stamp: str, log_name: str, port: int,
                 api_port: int, wait_s: int, *, fresh_env: bool = False) -> str:
    return (SCRIPT_TEMPLATE
            .replace("__SRC__", src_name)
            .replace("__STAMP__", stamp)
            .replace("__PORT__", str(port))
            .replace("__API__", str(api_port))
            .replace("__TRIES__", str(max(1, wait_s // 2)))
            .replace("__LOG__", log_name)
            .replace("__FRESH__", "1" if fresh_env else "0"))


async def _run_script(ip: str, port: int, from_fp: str, script_name: str) -> dict:
    """Start the upgrade detached, then hand control back immediately."""
    inner = (f"Start-Process powershell -ArgumentList '-NoProfile','-ExecutionPolicy',"
             f"'Bypass','-File','%USERPROFILE%\\Downloads\\{script_name}' "
             f"-WindowStyle Hidden; 'spawned'")
    command = f'powershell -NoProfile -ExecutionPolicy Bypass -Command "{inner}"'
    intent = shell_intent(from_fp, from_fp, "upgrade", command, 30)
    try:
        return await post_json(ip, "/intent", intent, port=port, timeout=30)
    except PeerError as exc:
        # The daemon usually dies right here; that is the expected restart.
        return {"ok": True, "detail": exc.kind}


async def _port_up(url: str) -> bool:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=3)) as r:
                return r.status < 500
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return False


async def upgrade(store, ident: dict, to: str, path: str, *,
                  wait_s: int = 60, port_arg: int | None = None,
                  fresh_env: bool = False) -> dict:
    pkg = verify_package(path)
    ip, port, fp = resolve_target(store, to)
    if not ip:
        return {"ok": False, "error": "no-device", "target": to}
    if not store.is_paired(fp):
        return {"ok": False, "error": "not-paired", "target": fp}
    dev_port = port or port_arg or DEVICE_PORT
    stamp = time.strftime("%Y%m%d-%H%M%S")
    src_name = f"RelayDesk-upgrade-{stamp}.exe"
    script_name = f"rd_upgrade_{stamp}.ps1"
    log_name = f"rd_upgrade_{stamp}.log"

    tmpdir = tempfile.mkdtemp(prefix="flix-upgrade-")
    try:
        local = os.path.join(tmpdir, src_name)
        shutil.copy2(pkg["path"], local)
        sent = await send_file(ip, local, from_fp=ident["fingerprint"],
                               to_fp=fp, port=dev_port)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    if not sent.get("ok"):
        return {"ok": False, "error": "upload-failed", "detail": sent}

    script_path = os.path.join(tmpdir + "-script", script_name)
    os.makedirs(os.path.dirname(script_path), exist_ok=True)
    with open(script_path, "w", encoding="utf-8") as fh:
        fh.write(build_script(src_name, stamp, log_name, dev_port,
                              dev_port + 7, wait_s, fresh_env=fresh_env))
    try:
        sent_script = await send_file(ip, script_path, from_fp=ident["fingerprint"],
                                      to_fp=fp, port=dev_port)
    finally:
        shutil.rmtree(os.path.dirname(script_path), ignore_errors=True)
    if not sent_script.get("ok"):
        return {"ok": False, "error": "script-upload-failed", "detail": sent_script}

    started = time.monotonic()
    await _run_script(ip, dev_port, ident["fingerprint"], script_name)
    # The old daemon is still listening at this point: wait until the port
    # actually drops (the script stops it), then for it to come back.
    url = f"http://{ip}:{dev_port}/"
    deadline = time.monotonic() + wait_s + 40
    went_down = False
    healthy = False
    while time.monotonic() < deadline:
        up = await _port_up(url)
        if not up:
            went_down = True
        elif went_down:
            healthy = True
            break
        await asyncio.sleep(2)
    elapsed = int((time.monotonic() - started) * 1000)
    log = await _read_log(ip, dev_port, ident["fingerprint"], log_name)
    reverted = "unhealthy-rollback" in log
    return {"ok": healthy, "target": fp, "ip": ip, "port": dev_port,
            "sha256": pkg["sha256"], "size": pkg["size"],
            "script": script_name, "elapsedMs": elapsed, "healthy": healthy,
            "reverted": reverted, "log": log}


async def _read_log(ip: str, port: int, from_fp: str, log_name: str) -> str:
    intent = shell_intent(from_fp, from_fp, "upgrade-log",
                          f"type %TEMP%\\{log_name}", 15)
    try:
        reply = await post_json(ip, "/intent", intent, port=port, timeout=20)
        return str(reply.get("stdout") or "")[-1500:]
    except PeerError:
        return ""
