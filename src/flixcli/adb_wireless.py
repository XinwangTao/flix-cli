"""Wireless ADB quick connect.

Android 11+ exposes a random TLS port only through mDNS
(`_adb-tls-connect._tcp`). On macOS the host also cannot dial the phone
directly: macOS 15's Local Network privacy gate blocks third-party binaries
(adb included), so the flow is discovery -> loopback relay -> adb connect.
The relay runs on Apple's python, which the gate allows.

Everything here is local-operator tooling: no remote tunnel, and adb commands
are still passed as argv lists by :class:`flixcli.adb.AdbClient`.
"""
from __future__ import annotations

import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from .config import APP_DIR

DEFAULT_RELAY_PORT = 40009
BROWSE_SERVICE = "_adb-tls-connect._tcp"
SYSTEM_PYTHON = "/usr/bin/python3"

# The relay is a separate process (Apple python, stdlib only), so it cannot be
# imported from the flixcli package: a frozen .app keeps modules inside the PYZ
# archive, where no runnable file exists. The source is materialised to a temp
# file instead, which works for both pip installs and PyInstaller bundles.
RELAY_SOURCE = '''"""Loopback TCP relay: 127.0.0.1:<listen> -> <target_host>:<target_port>."""
import socket
import sys
import threading


def pipe(a, b):
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except Exception:
        pass
    finally:
        try:
            b.shutdown(socket.SHUT_WR)
        except Exception:
            pass


def handle(client):
    try:
        upstream = socket.create_connection((TARGET_HOST, TARGET_PORT), timeout=10)
        upstream.settimeout(None)
        client.settimeout(None)
    except Exception as exc:
        print("upstream failed:", exc, flush=True)
        client.close()
        return
    worker = threading.Thread(target=pipe, args=(client, upstream), daemon=True)
    worker.start()
    pipe(upstream, client)
    worker.join(timeout=5)
    try:
        client.close()
        upstream.close()
    except Exception:
        pass


def main():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((LISTEN_HOST, LISTEN_PORT))
    except OSError as exc:
        print("bind failed:", exc, flush=True)
        return 1
    listener.listen(16)
    print("relay listening on %s:%d -> %s:%d"
          % (LISTEN_HOST, LISTEN_PORT, TARGET_HOST, TARGET_PORT), flush=True)
    while True:
        try:
            client, _ = listener.accept()
        except KeyboardInterrupt:
            return 0
        threading.Thread(target=handle, args=(client,), daemon=True).start()


if __name__ == "__main__":
    LISTEN_HOST = "127.0.0.1"
    LISTEN_PORT = int(sys.argv[1])
    TARGET_HOST = sys.argv[2]
    TARGET_PORT = int(sys.argv[3])
    sys.exit(main())
'''

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


class AdbWirelessError(RuntimeError):
    def __init__(self, kind: str, detail: str):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


# ---------- pure parsers (unit-tested without dns-sd) ----------

def parse_browse(output: str) -> str:
    """Instance name from `dns-sd -B` Add lines (last field)."""
    for line in reversed(output.splitlines()):
        if re.search(r"\sAdd\s", line):
            name = line.split()[-1].strip()
            if name:
                return name
    return ""


def parse_resolve(output: str) -> tuple[str, int] | None:
    """(host, port) from the `reached at host:port` line of `dns-sd -L`."""
    match = re.search(r"reached at ([^\s:]+):(\d+)", output)
    if match:
        return match.group(1), int(match.group(2))
    return None


def parse_ipv4(output: str) -> str:
    """First IPv4 in `dns-sd -G v4` output, skipping loopback/link-local."""
    for match in _IPV4.finditer(output):
        ip = match.group(0)
        if ip.startswith(("127.", "169.254.", "0.")):
            continue
        return ip
    return ""


def parse_adb_mdns(output: str) -> tuple[str, int, str] | None:
    """(host, port, instance) from `adb mdns services` on non-macOS hosts."""
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == BROWSE_SERVICE:
            host, _, port = parts[-1].rpartition(":")
            if host and port.isdigit():
                return host, int(port), parts[0]
    return None


# ---------- system helpers ----------

def _system_python() -> str:
    """Apple's python is exempt from the Local Network gate; prefer it.

    In a frozen bundle sys.executable is the app binary, so running it as an
    interpreter would just re-enter our own CLI. Refuse instead of spawning
    something wrong.
    """
    if os.path.exists(SYSTEM_PYTHON):
        return SYSTEM_PYTHON
    if getattr(sys, "frozen", False):
        raise AdbWirelessError(
            "relay-python", f"中继需要系统解释器 {SYSTEM_PYTHON}，冻结包内不可用")
    return sys.executable


def _run_dir() -> Path:
    """Private (0700) spot for the relay script, pid file and log.

    /tmp is world-writable; a predictable name there lets another local user
    pre-create the path and race us. Persisting under APP_DIR avoids that.
    """
    path = APP_DIR / "run"
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _run_dns(args: list[str], seconds: float, ready=None) -> str:
    """Run dns-sd for at most `seconds`, stopping as soon as `ready` is true.

    Fixed sleeps per lookup cost 3x the timeout on every connect; the parsers
    already know what a complete answer looks like, so exit as soon as it lands.
    """
    if shutil.which("dns-sd") is None:
        raise AdbWirelessError("no-dns-sd",
                               "dns-sd not found; wireless discovery needs macOS")
    try:
        # Binary + os.read: a buffered TextIOWrapper would swallow later lines
        # into user space, where select() can no longer see them and the
        # early-exit below would wait out the whole timeout.
        proc = subprocess.Popen(["dns-sd", *args], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
    except OSError as exc:
        raise AdbWirelessError("discovery-failed", f"dns-sd 启动失败: {exc}") from exc
    chunks: list[bytes] = []
    deadline = time.monotonic() + seconds
    fd = proc.stdout.fileno()
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                readable = select.select([fd], [], [], left)[0]
            except OSError:
                # Windows cannot select() on pipes; fall back to `adb mdns`.
                break
            if not readable:
                break
            data = os.read(fd, 4096)
            if not data:
                break
            chunks.append(data)
            if ready and ready(b"".join(chunks).decode("utf-8", "replace")):
                break
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
    return b"".join(chunks).decode("utf-8", "replace")


def discover(timeout: float = 3.0, adb_path: str | None = None) -> dict:
    """Find the phone's wireless debugging endpoint."""
    instance, host, port, ip = "", "", 0, ""
    if shutil.which("dns-sd"):
        instance = parse_browse(
            _run_dns(["-B", BROWSE_SERVICE, "local"], timeout,
                     ready=lambda out: bool(parse_browse(out))))
        if instance:
            resolved = None
            output = _run_dns(
                ["-L", instance, BROWSE_SERVICE, "local"], timeout,
                ready=lambda out: parse_resolve(out) is not None)
            resolved = parse_resolve(output)
            if resolved:
                host, port = resolved
                ip = parse_ipv4(_run_dns(
                    ["-G", "v4", host], timeout,
                    ready=lambda out: bool(parse_ipv4(out))))
    if not ip:
        # Linux/Windows: adb's own mDNS browser is the portable fallback.
        adb = adb_path or os.environ.get("ANDROID_ADB", "adb")
        try:
            out = subprocess.run([adb, "mdns", "services"], capture_output=True,
                                 text=True, timeout=max(timeout * 2, 5)).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        found = parse_adb_mdns(out)
        if found:
            ip, port, instance = found
    if not ip or not port:
        raise AdbWirelessError(
            "no-service",
            "未发现无线调试服务：请在手机上开启「无线调试」，并确认与电脑在同一 Wi-Fi")
    return {"instance": instance or host, "ip": ip, "port": int(port)}


def _pid_file(port: int) -> Path:
    return _run_dir() / f"relay_{port}.pid"


def stop_relay(port: int = DEFAULT_RELAY_PORT) -> bool:
    """Stop a relay we started earlier on this port, if any.

    The pid file can outlive a reboot, so the command line is re-checked before
    signalling: a recycled pid must never be killed by accident.
    """
    pid_file = _pid_file(port)
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        command = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                 capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        command = ""
    if "flix_adb_relay" in command:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    try:
        pid_file.unlink()
    except OSError:
        pass
    return True


def port_open(port: int, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _relay_script_path() -> Path:
    """Materialise RELAY_SOURCE to a private file (rewritten if it drifted)."""
    path = _run_dir() / "adb_relay_script.py"
    try:
        if path.read_text(encoding="utf-8") == RELAY_SOURCE:
            return path
    except OSError:
        pass
    path.write_text(RELAY_SOURCE, encoding="utf-8")
    return path


def start_relay(target_ip: str, target_port: int,
                listen_port: int = DEFAULT_RELAY_PORT,
                timeout: float = 5.0) -> dict:
    """Spawn the loopback relay and wait until it accepts connections.

    A port already taken by something we did not start (e.g. an older manual
    relay script on 40009) is left alone; the next free port is used instead so
    both keep working.
    """
    relay_script = _relay_script_path()
    busy = False
    chosen = 0
    for candidate in range(listen_port, listen_port + 10):
        stop_relay(candidate)
        if port_open(candidate):
            busy = True
            continue
        chosen = candidate
        break
    if not chosen:
        raise AdbWirelessError(
            "relay-port-busy",
            f"{listen_port}-{listen_port + 9} 全被占用"
            + ("（含用户已有的中继）" if busy else "") + "，换一个 --relay-port")
    listen_port = chosen
    log_path = _run_dir() / f"relay_{listen_port}.log"
    log = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 - lives with the child
    try:
        proc = subprocess.Popen(
            [_system_python(), "-I", str(relay_script), str(listen_port),
             target_ip, str(target_port)],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:
        log.close()
        raise AdbWirelessError("relay-failed", f"中继启动失败: {exc}") from exc
    # The child keeps the fd; our copy is only for the spawn.
    log.close()
    _pid_file(listen_port).write_text(str(proc.pid))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_open(listen_port):
            return {"pid": proc.pid, "listen": listen_port, "log": str(log_path)}
        if proc.poll() is not None:
            detail = ""
            try:
                detail = log_path.read_text(encoding="utf-8").strip()
            except OSError:
                pass
            raise AdbWirelessError("relay-failed",
                                   f"中继进程退出: {detail or proc.returncode}")
        time.sleep(0.1)
    proc.terminate()
    raise AdbWirelessError("relay-timeout",
                           f"中继 {timeout:.0f}s 内未监听 127.0.0.1:{listen_port}")


def connect(adb_path: str | None = None, relay_port: int = DEFAULT_RELAY_PORT,
            timeout: float = 6.0, adb_timeout: float = 30.0) -> dict:
    """Discover the phone, relay through loopback, and verify with `adb devices`."""
    adb = adb_path or os.environ.get("ANDROID_ADB", "adb")
    if shutil.which(adb) is None and not os.path.exists(adb):
        raise AdbWirelessError("adb-missing", f"找不到 adb: {adb}")
    found = discover(timeout=timeout, adb_path=adb)
    relay = start_relay(found["ip"], found["port"], relay_port, timeout=timeout)
    serial = f"127.0.0.1:{relay['listen']}"
    try:
        out = subprocess.run([adb, "connect", serial], capture_output=True,
                             text=True, timeout=adb_timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        stop_relay(relay["listen"])
        raise AdbWirelessError("connect-failed", f"adb connect 失败: {exc}") from exc
    devices_out = ""
    try:
        listed = subprocess.run([adb, "devices"], capture_output=True, text=True,
                                timeout=adb_timeout)
        devices_out = listed.stdout
    except (OSError, subprocess.SubprocessError):
        pass
    ok = any(line.split()[:1] == [serial] and "device" in line.split()[1:2]
             for line in devices_out.splitlines())
    if not ok:
        # A relay nobody talks to is a leaked process: tear it down before
        # reporting the failure, so a retry starts from a clean slate.
        stop_relay(relay["listen"])
        raise AdbWirelessError(
            "connect-failed",
            (out.stdout or out.stderr or "adb connect 未返回成功").strip())
    return {"ok": True, "serial": serial, "device": found,
            "relay": relay, "adb": out.stdout.strip()}
