"""Self-update from GitHub releases (used by `flix update`).

Three facts shape this module, all verified against real releases:

* The published asset is **one 95 MB `RelayDesk.exe` inside a zip**, so an
  update is a single-file swap, not a tree copy.
* Windows refuses to overwrite a running exe but allows a rename, so the old
  binary is renamed to `.prev-<stamp>` and restored if the new build never
  comes up.
* A `console=False` PyInstaller build prints nothing to the console, so
  `RelayDesk.exe --version` cannot identify the installed build. Identity comes
  from a state file keyed by the exe path, with the release asset's own
  `sha256:` digest as the authoritative "already installed" check.

GitHub's unauthenticated API allows 60 requests/hour per IP, which is why the
result of every check is cached in `state` and why `--token` exists.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import APP_DIR

REPO_DEFAULT = "XinwangTao/flix-cli"
API_DEFAULT = "https://api.github.com"
# Downloads from github.com are usable here (~1 MB/s) but stall on some
# networks; these prefixes are tried only after a direct attempt fails.
MIRRORS = ("https://gh-proxy.com/", "https://ghfast.top/")

EXE_NAME = "RelayDesk.exe"
STATE_PATH = APP_DIR / "update_state.json"
LOG_PATH = APP_DIR / "update.log"
LOCK_PATH = APP_DIR / "update.lock"
BACKUP_KEEP = 3
LOCK_STALE_S = 45 * 60

EXIT_OK, EXIT_NETWORK, EXIT_INSTALL, EXIT_ENV = 0, 3, 4, 5
EXIT_UPDATE_AVAILABLE = 10

# The lite pattern must not match the "-full" download: two zips ship per
# release and the default channel only carries the one without the media stack.
_PLATFORM_ASSET = {
    "win32": r"win(?:64|32)?(?![^/]*full)[-_.]?.*\.zip$",
    "darwin": r"(?:macos|osx|darwin).*\.zip$",
    "linux": r"linux.*\.zip$",
}
# A machine that installed the full package keeps getting the full package;
# silently falling back to lite would take screen sharing away on update.
_FULL_ASSET = {
    "win32": r"win(?:64|32)?[-_.].*full.*\.zip$",
}


def preferred_asset_pattern(record: dict | None) -> str | None:
    """Sticky channel: full stays full, everything else gets the default."""
    if sys.platform not in _FULL_ASSET:
        return None
    asset = (record or {}).get("asset") or ""
    if "full" in str(asset).lower():
        return _FULL_ASSET[sys.platform]
    return None


def log_line(message: str, *, path: Path | None = None, echo: bool = False) -> None:
    """Append to the updater log; scheduled runs have no console to print to."""
    path = path or LOG_PATH
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1 << 20:
            with contextlib.suppress(OSError):
                os.replace(path, path.with_suffix(".log.1"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except OSError:
        pass
    if echo:
        # The caller may be the GUI we just stopped to swap its exe: its end of
        # the stdout pipe is gone, and a failed print must never abort a swap or
        # (worse) the rollback that follows an unhealthy restart.
        try:
            print(message, flush=True)
        except (OSError, ValueError):
            pass


# ---------------------------------------------------------------- versions

def parse_version(text: str | None) -> tuple[int, ...]:
    """`v2.2.1`, `2.4.0+127` and `flix 2.2.1` all mean (2, 2, 1)."""
    if not text:
        return ()
    match = re.search(r"(\d+(?:\.\d+)*)", str(text))
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def compare_versions(left: str | tuple, right: str | tuple) -> int:
    """-1 / 0 / 1; 2.2 and 2.2.0 are equal, empty losers compare as 0."""
    a = parse_version(left) if isinstance(left, str) else left
    b = parse_version(right) if isinstance(right, str) else right
    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))
    return (a > b) - (a < b)


# ---------------------------------------------------------------- releases

@dataclass
class Asset:
    name: str
    size: int
    url: str
    sha256: str = ""

    @classmethod
    def from_api(cls, raw: dict) -> "Asset":
        digest = str(raw.get("digest") or "")
        sha = digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else ""
        try:
            size = int(raw.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        return cls(name=str(raw.get("name") or ""), size=size,
                   url=str(raw.get("browser_download_url") or ""), sha256=sha)


@dataclass
class Release:
    tag: str
    name: str
    prerelease: bool
    draft: bool
    url: str
    body: str = ""
    assets: list[Asset] = field(default_factory=list)

    @property
    def version(self) -> tuple[int, ...]:
        return parse_version(self.tag)

    @classmethod
    def from_api(cls, raw: dict) -> "Release":
        raw_assets = raw.get("assets")
        if not isinstance(raw_assets, list):
            raw_assets = []
        return cls(tag=str(raw.get("tag_name") or ""),
                   name=str(raw.get("name") or ""),
                   prerelease=bool(raw.get("prerelease")),
                   draft=bool(raw.get("draft")),
                   url=str(raw.get("html_url") or ""),
                   body=str(raw.get("body") or "")[:2000],
                   assets=[Asset.from_api(a) for a in raw_assets
                           if isinstance(a, dict)])


class UpdateError(RuntimeError):
    """Carries an exit code so the CLI can map failures without guessing."""

    def __init__(self, kind: str, detail: str, code: int = EXIT_ENV):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.code = code


def _open(url: str, *, token: str | None = None, timeout: float = 20.0):
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"flixcli-autoupdate/{sys.platform}",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    })
    return urllib.request.urlopen(request, timeout=timeout)


def fetch_releases(repo: str = REPO_DEFAULT, *, api_base: str = API_DEFAULT,
                   token: str | None = None, per_page: int = 100,
                   mirrors: tuple[str, ...] = MIRRORS) -> list[Release]:
    """Newest-first releases, or UpdateError('network') with the reason kept.

    GitHub caps a page at 100. Asking for only 10 could hide the latest stable
    release behind ten newer prereleases or drafts, so the updater would never
    offer it.
    """
    per_page = max(1, min(int(per_page), 100))
    url = f"{api_base.rstrip('/')}/repos/{repo}/releases?per_page={per_page}"
    attempts = [url] + [f"{m.rstrip('/')}/{url}" for m in mirrors]
    errors: list[str] = []
    not_found = 0
    for candidate in attempts:
        try:
            with _open(candidate, token=token) as response:
                payload = json.loads(response.read().decode("utf-8"))
            # A mirror can return a 200 with a rate-limit/error object. Without
            # this check the list comprehension dies with AttributeError and
            # the remaining mirrors are never tried.
            if not isinstance(payload, list) or not all(
                    isinstance(item, dict) for item in payload):
                raise ValueError(
                    f"unexpected releases payload ({type(payload).__name__})")
            return [Release.from_api(item) for item in payload]
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # A middlebox/proxy can 404 the direct API; the next mirror may
                # still work. Only report no-releases when every attempt 404s.
                not_found += 1
                errors.append(f"{candidate} -> HTTP 404")
                continue
            hint = ""
            if exc.code in (403, 429):
                reset = exc.headers.get("X-RateLimit-Reset") if exc.headers else None
                hint = "（API 限流，设置 GITHUB_TOKEN 或 --token 可解除）"
                if reset:
                    hint += f"，重置时间 {time.strftime('%H:%M', time.localtime(int(reset)))}"
            errors.append(f"{candidate} -> HTTP {exc.code} {hint}")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            errors.append(f"{candidate} -> {exc}")
    if errors and not_found == len(attempts):
        raise UpdateError("no-releases", f"{repo} 没有 release", EXIT_NETWORK)
    raise UpdateError("network", "; ".join(errors) or "所有镜像均不可用", EXIT_NETWORK)


def pick_release(releases: list[Release], *, include_prerelease: bool = False) -> Release:
    """Highest published version, not merely the most recently created one.

    The GitHub API returns ``created_at`` order. A backport published after a
    newer line (e.g. 2.3.1 then a 2.3.0 rebuild) therefore appears first, and
    taking that entry would hide the higher version forever. Version ties go to
    the stable tag so ``--prerelease`` still prefers 2.3.0 over 2.3.0-rc1.
    """
    eligible = [r for r in releases
                if not r.draft and (include_prerelease or not r.prerelease)]
    if not eligible:
        raise UpdateError("no-releases", "没有可用的正式 release", EXIT_NETWORK)
    return max(eligible, key=lambda r: (r.version, not r.prerelease))


def pick_asset(release: Release, *, pattern: str | None = None) -> Asset:
    pattern = pattern or _PLATFORM_ASSET.get(sys.platform, r"\.zip$")
    regex = re.compile(pattern, re.IGNORECASE)
    # Sorted so the same release always yields the same asset across runs.
    for asset in sorted(release.assets, key=lambda a: a.name):
        if regex.search(asset.name):
            return asset
    names = ", ".join(a.name for a in release.assets) or "（无资产）"
    raise UpdateError("no-asset", f"{release.tag} 无匹配 {pattern} 的资产: {names}", EXIT_NETWORK)


# ---------------------------------------------------------------- download

def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_once(url: str, dest: Path, offset: int, *, timeout: float) -> int:
    """Returns bytes written. A 200 response means Range was ignored → restart."""
    headers = {"User-Agent": "flixcli-autoupdate"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        mode = "ab"
        if offset and response.status != 206:
            mode, offset = "wb", 0
        written = 0
        with dest.open(mode) as fh:
            for chunk in iter(lambda: response.read(1 << 20), b""):
                fh.write(chunk)
                written += len(chunk)
        return written


def download_asset(asset: Asset, dest: str | Path, *, mirrors: tuple[str, ...] = MIRRORS,
                   retries: int = 3, timeout: float = 60.0,
                   progress=None) -> Path:
    """Resume-capable download; the caller verifies the hash, not this function."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    attempts = [asset.url] + [f"{m.rstrip('/')}/{asset.url}" for m in mirrors]
    problems: list[str] = []
    for url in attempts:
        for attempt in range(retries):
            offset = dest.stat().st_size if dest.exists() else 0
            if asset.size and offset > asset.size:
                dest.unlink()
                offset = 0
            if asset.size and offset == asset.size:
                return dest
            try:
                written = _download_once(url, dest, offset, timeout=timeout)
                if progress:
                    progress(dest.stat().st_size, asset.size)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
                    OSError) as exc:
                problems.append(f"{url} -> {exc}")
                time.sleep(min(2 ** attempt, 8))
                continue
            if not asset.size or dest.stat().st_size >= asset.size:
                return dest
            if not written:
                problems.append(f"{url} -> 0 bytes (已收 {dest.stat().st_size})")
                break
        if asset.size and dest.exists() and dest.stat().st_size == asset.size:
            return dest
    size = dest.stat().st_size if dest.exists() else 0
    raise UpdateError("download", f"下载未完成 ({size}/{asset.size or '?'}): "
                                 + "; ".join(problems[-3:]), EXIT_NETWORK)


# ---------------------------------------------------------------- payload

@dataclass
class Payload:
    kind: str          # "exe" (single binary) or "dir" (tree next to the exe)
    path: Path         # exe file, or the extracted tree root


def _inside(root: Path, path: Path) -> bool:
    """True when ``path`` is ``root`` or one of its descendants."""
    return path == root or root in path.parents


def _safe_extract_zip(archive: Path, workdir: Path) -> list[str]:
    """Extract a release zip, refusing members that escape ``workdir``.

    The zip comes from the network and auto-update runs as root on a board, so
    a member named ``../x`` (zip-slip) must never be written outside the
    disposable staging directory. Returns the file members for layout detection.
    """
    workdir = workdir.resolve()
    try:
        with zipfile.ZipFile(archive) as zf:
            bad = zf.testzip()
            if bad:
                raise UpdateError("bad-archive", f"zip 损坏: {bad}", EXIT_INSTALL)
            for info in zf.infolist():
                target = (workdir / info.filename).resolve()
                if not _inside(workdir, target):
                    raise UpdateError("bad-archive",
                                      f"zip 成员越界: {info.filename}", EXIT_INSTALL)
            names = [info.filename for info in zf.infolist() if not info.is_dir()]
            zf.extractall(workdir)
            return names
    except UpdateError:
        raise
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise UpdateError("bad-archive", f"zip 无法解包: {exc}", EXIT_INSTALL) from exc


def _check_tar_members(tar: tarfile.TarFile, workdir: Path) -> None:
    """Fallback containment checks for interpreters without ``data_filter``."""
    for member in tar.getmembers():
        target = (workdir / member.name).resolve()
        if not _inside(workdir, target):
            raise UpdateError("bad-archive",
                              f"tar 成员越界: {member.name}", EXIT_INSTALL)
        if member.isdev():
            raise UpdateError("bad-archive",
                              f"tar 含设备节点: {member.name}", EXIT_INSTALL)
        if member.issym() or member.islnk():
            link = (target.parent / member.linkname).resolve()
            if not _inside(workdir, link):
                raise UpdateError(
                    "bad-archive",
                    f"tar 链接越界: {member.name} -> {member.linkname}",
                    EXIT_INSTALL)


def _safe_extract_tar(archive: Path, workdir: Path) -> None:
    """Extract a source tarball with the same containment guarantee as zips.

    Python 3.11.4+/3.12 (what the board runs) has ``filter="data"``: reject
    absolute paths, ``..``, device nodes and links that leave the destination.
    Older interpreters get the same checks by hand.
    """
    workdir = workdir.resolve()
    try:
        with tarfile.open(archive, "r:gz") as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(workdir, filter="data")
                return
            _check_tar_members(tar, workdir)
            tar.extractall(workdir)
    except UpdateError:
        raise
    except (tarfile.TarError, OSError, ValueError) as exc:
        raise UpdateError("bad-archive", f"tar 无法解包: {exc}", EXIT_INSTALL) from exc


def extract_payload(archive: str | Path, workdir: str | Path, *,
                    exe_name: str = EXE_NAME) -> Payload:
    """Unpack and decide whether the zip is one exe or a directory tree.

    Both layouts exist in this repo's history: v2.2.1 ships a bare exe, older
    notes describe a `RelayDesk-v<x>-win64/` folder wrapping it.
    """
    archive, workdir = Path(archive), Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    names = _safe_extract_zip(archive, workdir)
    target = exe_name.lower()
    matches = [n for n in names if Path(n).name.lower() == target]
    if not matches:
        raise UpdateError("no-payload", f"zip 内没有 {exe_name}: {names[:6]}", EXIT_INSTALL)
    if len(names) == 1:
        return Payload("exe", workdir / names[0])
    # Directory layout: the tree to copy is the folder that holds the exe.
    root = Path(matches[0]).parent
    return Payload("dir", workdir / root if str(root) != "." else workdir)


def validate_payload(payload: Payload, *, min_bytes: int = 1 << 20) -> None:
    """Cheap pre-flight: a truncated or HTML-error download must not replace
    a working exe. PE magic covers Windows; the size floor covers both."""
    exe = payload.path if payload.kind == "exe" else payload.path / EXE_NAME
    if not exe.is_file():
        raise UpdateError("bad-payload", f"缺少 {exe.name}", EXIT_INSTALL)
    size = exe.stat().st_size
    if size < min_bytes:
        raise UpdateError("bad-payload", f"{exe.name} 只有 {size} 字节，疑似下载被截断",
                          EXIT_INSTALL)
    with exe.open("rb") as fh:
        magic = fh.read(2)
    if sys.platform == "win32" and magic != b"MZ":
        raise UpdateError("bad-payload", f"{exe.name} 不是 PE 文件 (magic={magic!r})",
                          EXIT_INSTALL)


# ---------------------------------------------------------------- state

def load_state(path: str | Path = STATE_PATH) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict, path: str | Path = STATE_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _state_key(exe: str | Path) -> str:
    return str(Path(exe).resolve()).lower()


def installed_record(state: dict, exe: str | Path) -> dict:
    return (state.get("installs") or {}).get(_state_key(exe)) or {}


def remember_install(state: dict, exe: str | Path, *, tag: str, asset: str,
                     sha256: str, exe_sha256: str, source: str) -> dict:
    state.setdefault("installs", {})[_state_key(exe)] = {
        "tag": tag, "asset": asset, "sha256": sha256,
        "exe_sha256": exe_sha256, "source": source,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    return state


# ---------------------------------------------------------------- target

def detect_target(*, explicit: str | None = None, platform: str | None = None) -> Path:
    """Where the installed RelayDesk.exe lives.

    Order matters: an explicit path wins, then the binary we are running inside
    (frozen build), then whatever is actually running, then the two places
    builds land on this machine.
    """
    platform = platform or sys.platform
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise UpdateError("no-target", f"找不到 {path}", EXIT_ENV)
        return path
    env = os.environ.get("RELAYDESK_EXE")
    if env and Path(env).is_file():
        return Path(env)
    if getattr(sys, "frozen", False) and Path(sys.executable).name.lower() == EXE_NAME.lower():
        return Path(sys.executable)
    for instance in list_instances(exe_name=EXE_NAME):
        if instance.get("exe"):
            return Path(instance["exe"])
    candidates = [Path.cwd() / "dist" / EXE_NAME]
    if platform == "win32":
        home = Path(os.environ.get("USERPROFILE") or Path.home())
        candidates += sorted((home / "Downloads").glob(f"RelayDesk-v*-win64/{EXE_NAME}"),
                             reverse=True)
        candidates += sorted((home / "Desktop").glob(f"RelayDesk*/{EXE_NAME}"), reverse=True)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise UpdateError("no-target", "找不到 RelayDesk.exe，请用 --exe 指定", EXIT_ENV)


# ---------------------------------------------------------------- processes

def _run_powershell(script: str, timeout: float = 20.0) -> str:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=flags)
    return completed.stdout


def list_instances(*, exe_name: str = EXE_NAME, exe: str | Path | None = None) -> list[dict]:
    """Running copies as {pid, exe, args}. Empty off-Windows."""
    if sys.platform != "win32":
        return []
    script = (
        f"Get-CimInstance Win32_Process -Filter \"Name='{exe_name}'\" | "
        "Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress")
    try:
        raw = _run_powershell(script).strip()
    except (OSError, subprocess.SubprocessError):
        return []
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        return []
    if isinstance(items, dict):
        items = [items]
    wanted = Path(exe).resolve() if exe else None
    found = []
    for item in items:
        path = item.get("ExecutablePath") or ""
        if wanted and path and Path(path).resolve() != wanted:
            continue
        command = item.get("CommandLine") or ""
        args = command.split(str(Path(path).name), 1)[-1].strip() if path else ""
        found.append({"pid": int(item.get("ProcessId") or 0), "exe": path, "args": args})
    return found


# Roles that outlive a single command and can be safely stopped and relaunched
# with the same arguments. Everything else is left alone on purpose:
#   * `mcp` speaks JSON-RPC over the client's stdio pipes. Restarting it
#     detached gives the editor a dead server and leaves an orphan.
#   * one-shot commands (send, pair, probe, `update` itself) either finish on
#     their own or must not be interrupted.
# A bare launch is the desktop window.
LONG_LIVED_ROLES = ("serve", "agent", "gui")


def managed_instances(instances: list[dict], *, self_pid: int | None = None) -> list[dict]:
    """The subset the updater may stop and restart.

    Excluding our own PID is not cosmetic: when the GUI (or a scheduled task)
    runs the frozen `RelayDesk.exe update`, that process has the same image path
    as the target and would otherwise be in the kill list.
    """
    self_pid = os.getpid() if self_pid is None else self_pid
    managed = []
    for instance in instances:
        if int(instance.get("pid") or 0) in (0, self_pid):
            continue
        args = (instance.get("args") or "").strip()
        if not args:
            managed.append(instance)          # bare launch = desktop window
            continue
        if args.split()[0].lower() in LONG_LIVED_ROLES:
            managed.append(instance)
    return managed


def stop_instances(instances: list[dict], *, timeout: float = 12.0) -> list[int]:
    """Graceful first (the daemon owns a sqlite store), then force."""
    if not instances or sys.platform != "win32":
        return [i["pid"] for i in instances]
    pids = [str(i["pid"]) for i in instances if i.get("pid")]
    if not pids:
        return []
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for pid in pids:
        subprocess.run(["taskkill", "/PID", pid], capture_output=True,
                       creationflags=flags)
    deadline = time.monotonic() + timeout
    alive = set(pids)
    while alive and time.monotonic() < deadline:
        time.sleep(0.4)
        alive = {p for p in alive if _pid_alive(p)}
    for pid in alive:
        subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True,
                       creationflags=flags)
    return [int(p) for p in pids]


def _pid_alive(pid: str) -> bool:
    try:
        completed = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=10)
        return str(pid) in (completed.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return False


def _clean_env() -> dict:
    """PyInstaller export vars point the child at the OLD extraction dir; keeping
    them makes a fresh build silently reuse stale unpacked files."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("_PYI") and not k.startswith("_MEI")}
    return env


def start_instance(exe: str | Path, args: str = "") -> subprocess.Popen | None:
    """Detached so the updater can exit without killing what it started.

    On Windows the command line is passed as one string: it came from the
    running process verbatim, and splitting it on spaces would break
    `agent --cmd "a command with quoted arguments"`.
    """
    exe = str(exe)
    if sys.platform == "win32":
        command: str | list[str] = f'"{exe}"' + (f" {args}" if args.strip() else "")
    else:
        command = [exe] + (args.split() if args else [])
    kwargs: dict = {"cwd": str(Path(exe).parent), "env": _clean_env(),
                    "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL, "close_fds": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        return subprocess.Popen(command, **kwargs)
    except OSError:
        return None


def _port_of(args: str, flag: str, default: int) -> int:
    match = re.search(rf"{flag}\s+(\d+)", args or "")
    return int(match.group(1)) if match else default


def wait_healthy(restarted: list[dict], *, timeout: float = 60.0) -> bool:
    """A `serve` instance must bind its API port again; a GUI copy only has to
    survive the window where a broken build dies on startup."""
    if not restarted:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(_crashed(item) for item in restarted):
            return False
        if all(_instance_ok(item) for item in restarted):
            return True
        time.sleep(1.0)
    return False


def _crashed(item: dict) -> bool:
    process = item.get("process")
    return process is not None and process.poll() is not None


def _instance_ok(item: dict) -> bool:
    if item.get("process") is None:
        return False  # spawn failed; nothing is running to be healthy
    if "serve" in (item.get("args") or ""):
        return _tcp_open(_port_of(item["args"], "--api-port", 8898))
    return time.monotonic() - item.get("started", 0.0) >= item.get("grace", 5.0)


def _tcp_open(port: int, host: str = "127.0.0.1", timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------- install

def swap_exe(current: str | Path, new: str | Path, *,
             stamp: str | None = None) -> Path:
    """Replace a non-running exe, keeping the old one next to it.

    Rename-then-copy (not in-place overwrite) so a failed copy leaves the
    backup intact and the rollback path is a plain rename back.
    """
    current, new = Path(current), Path(new)
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    backup = current.with_name(f"{current.name}.prev-{stamp}")
    if backup.exists():
        backup.unlink()
    os.replace(current, backup)
    try:
        shutil.copy2(new, current)
    except OSError:
        with contextlib.suppress(OSError):
            os.replace(backup, current)
        raise
    return backup


def swap_tree(current_dir: str | Path, payload_root: str | Path, *,
              stamp: str | None = None) -> list[Path]:
    """Directory layout: back up each overwritten file, then copy the tree."""
    current_dir, payload_root = Path(current_dir), Path(payload_root)
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    backups: list[Path] = []
    for source in sorted(payload_root.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(payload_root)
        destination = current_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            backup = destination.with_name(f"{destination.name}.prev-{stamp}")
            with contextlib.suppress(OSError):
                os.replace(destination, backup)
            backups.append(backup)
        shutil.copy2(source, destination)
    return backups


def prune_backups(folder: str | Path, keep: int = BACKUP_KEEP) -> list[str]:
    removed: list[str] = []
    backups = sorted(Path(folder).glob(f"{EXE_NAME}.prev-*"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in backups[keep:]:
        with contextlib.suppress(OSError):
            stale.unlink()
            removed.append(str(stale))
    return removed


# ---------------------------------------------------------------- lock

class UpdateLock:
    """One updater at a time: the scheduled task and a manual run can overlap."""

    def __init__(self, path: str | Path = LOCK_PATH, stale_after: float = LOCK_STALE_S):
        self.path = Path(path)
        self.stale_after = stale_after
        self.acquired = False

    def __enter__(self) -> "UpdateLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and time.time() - self.path.stat().st_mtime > self.stale_after:
            with contextlib.suppress(OSError):
                self.path.unlink()
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise UpdateError("busy", "另一个更新进程正在运行（update.lock 存在）", EXIT_INSTALL)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()} {time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
        self.acquired = True
        return self

    def __exit__(self, *_exc) -> None:
        if self.acquired:
            with contextlib.suppress(OSError):
                self.path.unlink()


# ---------------------------------------------------------------- decision

def decide(record: dict, local_tag: str | None, release: Release,
           asset: Asset) -> dict:
    """Whether to install, and why.

    The recorded asset digest outranks tag arithmetic: it proves the installed
    build is byte-for-byte the published release, even if the tag was retyped in
    a CHANGELOG. A missing record only costs one idempotent reinstall.
    """
    if asset.sha256 and record.get("sha256", "").lower() == asset.sha256.lower():
        return {"status": "current", "reason": "asset-digest-match"}
    if local_tag and release.tag == local_tag:
        return {"status": "current", "reason": "tag-match"}
    if not local_tag:
        return {"status": "update-available", "reason": "unknown-local-build"}
    gap = compare_versions(release.version, local_tag)
    if gap == 0:
        return {"status": "current", "reason": "same-version"}
    if gap < 0:
        return {"status": "current", "reason": f"local-newer ({local_tag})"}
    return {"status": "update-available", "reason": "newer-release"}


# ---------------------------------------------------------------- orchestrator

def run_update(*, repo: str = REPO_DEFAULT, api_base: str = API_DEFAULT,
               token: str | None = None, mirror_list: tuple[str, ...] = MIRRORS,
               exe: str | None = None, check_only: bool = False,
               force: bool = False, dry_run: bool = False, restart: bool = True,
               include_prerelease: bool = False, asset_pattern: str | None = None,
               wait_s: float = 60.0, installed_version: str | None = None,
               start_when_idle: bool = False,
               state_path: str | Path = STATE_PATH,
               log_path: str | Path = LOG_PATH, echo: bool = False,
               downloads_dir: str | Path | None = None) -> dict:
    """Check → download → verify → swap → restart → verify, with rollback."""
    target = detect_target(explicit=exe)
    state = load_state(state_path)
    record = installed_record(state, target)
    local_sha = sha256_file(target)
    local_tag = record.get("tag")
    if not local_tag:
        local_tag = installed_version or _probe_version(target)

    release = pick_release(fetch_releases(repo, api_base=api_base, token=token,
                                         mirrors=mirror_list),
                           include_prerelease=include_prerelease)
    asset = pick_asset(release, pattern=asset_pattern
                       or preferred_asset_pattern(record))
    verdict = decide(record, local_tag, release, asset)
    result = {"ok": True, "exe": str(target), "current": local_tag or "unknown",
              "currentSha256": local_sha,
              "latest": release.tag, "asset": asset.name, "assetSize": asset.size,
              "assetSha256": asset.sha256, "releaseUrl": release.url,
              "status": verdict["status"], "reason": verdict["reason"]}
    log_line(f"check exe={target} local={local_tag or 'unknown'} "
             f"latest={release.tag} -> {verdict['status']} ({verdict['reason']})",
             path=log_path, echo=echo)

    if verdict["status"] == "current" and not force:
        return result
    if check_only:
        result["planned"] = {"wouldInstall": release.tag, "asset": asset.name,
                             "exe": str(target)}
        return result

    with UpdateLock():
        workdir = Path(downloads_dir or Path(tempfile.gettempdir()) / "relaydesk-update")
        archive = workdir / asset.name
        if archive.exists() and asset.sha256 and sha256_file(archive) == asset.sha256:
            log_line(f"reuse verified archive {archive}", path=log_path, echo=echo)
        else:
            log_line(f"download {asset.url} ({asset.size} bytes)", path=log_path, echo=echo)
            download_asset(asset, archive, mirrors=mirror_list)
        archive_sha = sha256_file(archive)
        if asset.sha256 and archive_sha != asset.sha256:
            with contextlib.suppress(OSError):
                archive.unlink()
            raise UpdateError("checksum", f"zip sha256 不符: 期望 {asset.sha256} "
                                          f"实际 {archive_sha}", EXIT_INSTALL)
        result["sha256"] = archive_sha
        if not asset.sha256:
            log_line(f"warning: {asset.name} 无 digest，跳过哈希校验", path=log_path, echo=echo)

        staging = workdir / "staging"
        shutil.rmtree(staging, ignore_errors=True)
        payload = extract_payload(archive, staging)
        validate_payload(payload)

        if dry_run:
            # The rehearsal has to reach the same verdict the real run would:
            # a bad digest, a truncated zip or a zip without the exe must fail
            # here, not on the machine of whoever runs it for real.
            result.update({"status": "dry-run", "layout": payload.kind,
                           "verified": True,
                           "planned": {"wouldInstall": release.tag,
                                       "asset": asset.name,
                                       "exe": str(target)}})
            log_line(f"dry-run verified {release.tag} layout={payload.kind} "
                     f"sha256={archive_sha[:12]} (nothing installed)",
                     path=log_path, echo=echo)
            return result

        instances = list_instances(exe=target) if sys.platform == "win32" else []
        managed = managed_instances(instances)
        skipped = [i for i in instances if i not in managed]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        result.update({"layout": payload.kind, "stopped": [i["pid"] for i in managed],
                       "leftRunning": [i["pid"] for i in skipped]})
        log_line(f"install {release.tag} layout={payload.kind} "
                 f"managed={len(managed)} left_running={len(skipped)}",
                 path=log_path, echo=echo)
        if managed:
            stop_instances(managed)

        backups: list[Path] = []
        if payload.kind == "exe":
            backups.append(swap_exe(target, payload.path, stamp=stamp))
        else:
            backups.extend(swap_tree(target.parent, payload.path, stamp=stamp))
        result["backups"] = [str(b) for b in backups]
        new_sha = sha256_file(target)

        restarted: list[dict] = []
        if restart and managed:
            for instance in managed:
                process = start_instance(target, instance.get("args") or "")
                restarted.append({"process": process, "args": instance.get("args") or "",
                                  "started": time.monotonic(), "grace": 5.0})
        result["restarted"] = [r["process"].pid for r in restarted if r["process"]]
        healthy = wait_healthy(restarted, timeout=wait_s)

        if not healthy:
            log_line("unhealthy -> rollback", path=log_path, echo=echo)
            for item in restarted:
                if item["process"] is not None and item["process"].poll() is None:
                    with contextlib.suppress(OSError):
                        item["process"].kill()
            _restore(backups, target)
            for instance in managed:
                start_instance(target, instance.get("args") or "")
            return {"ok": False, "error": "unhealthy-rollback", "exe": str(target),
                    "backups": [str(b) for b in backups],
                    "restored": local_tag or "unknown"}

        remember_install(state, target, tag=release.tag, asset=asset.name,
                         sha256=archive_sha, exe_sha256=new_sha, source=asset.url)
        state["last_check"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        save_state(state, state_path)
        pruned = prune_backups(target.parent)
        result.update({"status": "installed", "installed": release.tag,
                       "exeSha256": new_sha, "healthy": healthy, "pruned": pruned})
        if start_when_idle and not managed:
            launched = start_instance(target)
            result["started"] = launched.pid if launched else None
            log_line(f"start-after-update pid={result['started']}",
                     path=log_path, echo=echo)
        log_line(f"installed {release.tag} healthy={healthy} pruned={len(pruned)}",
                 path=log_path, echo=echo)
        # Keep the verified archive for a re-run, drop only the staging tree.
        shutil.rmtree(staging, ignore_errors=True)
        return result


def _restore(backups: list[Path], target: Path) -> None:
    """Undo a swap: exe backups replace the target, tree backups revert in place."""
    for backup in backups:
        if backup.name.startswith(f"{EXE_NAME}.prev-"):
            with contextlib.suppress(OSError):
                os.replace(backup, target)
            continue
        original = backup.with_name(backup.name.rsplit(".prev-", 1)[0])
        with contextlib.suppress(OSError):
            os.replace(backup, original)


def _probe_version(exe: Path) -> str | None:
    """Best effort: a windowed build prints nowhere, so None is normal."""
    try:
        completed = subprocess.run([str(exe), "--version"], capture_output=True,
                                   text=True, encoding="utf-8", errors="replace",
                                   timeout=15, cwd=str(exe.parent))
    except (OSError, subprocess.SubprocessError):
        return None
    text = (completed.stdout or "") + (completed.stderr or "")
    version = parse_version(text)
    return ".".join(str(p) for p in version) if version else None

# ---------------------------------------------------------------- startup check

def check_update(repo: str = REPO_DEFAULT, current: str | None = None,
                 *, token: str | None = None) -> dict:
    """Latest release vs installed version; never raises.

    flixd calls this at every start so a stale daemon on a board says so in its
    own log and in /healthz instead of silently running last month's build.
    A token avoids GitHub's 60 requests/hour per-IP limit; an explicit value
    wins, otherwise GITHUB_TOKEN / FLIX_GITHUB_TOKEN is read.
    """
    from . import __version__ as installed

    token = (token or os.environ.get("GITHUB_TOKEN")
             or os.environ.get("FLIX_GITHUB_TOKEN") or None)
    cur = current or installed
    try:
        release = pick_release(fetch_releases(repo, token=token))
    except UpdateError as exc:
        return {"ok": False, "error": exc.kind, "detail": exc.detail, "current": cur}
    return {
        "ok": True,
        "current": cur,
        "latest": release.tag.lstrip("vV"),
        "tag": release.tag,
        "available": compare_versions(release.tag, cur) > 0,
        "url": release.url,
    }


def _source_version(root: Path) -> str | None:
    """Static ``[project] version`` from an extracted source tree, if any."""
    try:
        import tomllib
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (ImportError, OSError, ValueError):
        return None
    project = data.get("project") if isinstance(data, dict) else None
    version = project.get("version") if isinstance(project, dict) else None
    return str(version) if version else None


def install_from_source(tag: str, repo: str = REPO_DEFAULT,
                        workdir: str | Path | None = None) -> str:
    """Download the GitHub source tarball for `tag` and install it in place.

    Used on Linux boxes that run from source (board deployments): the published
    binaries are x86_64/Windows only, so the source tarball is the update.
    Returns the installed version string.

    Each run gets a fresh private directory; ``workdir`` is only its parent.
    A fixed, world-writable staging path (the old ``/tmp/flixcli-update``) could
    be pre-created as a symlink and have its target wiped before extraction.
    """
    parent = Path(workdir).expanduser() if workdir else None
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="flixcli-update-",
                                 dir=str(parent) if parent else None))
    try:
        url = f"https://api.github.com/repos/{repo}/tarball/{tag}"
        archive = work / "source.tar.gz"
        with _open(url) as resp, open(archive, "wb") as fh:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                fh.write(chunk)
        _safe_extract_tar(archive, work)
        roots = [p for p in work.iterdir() if p.is_dir()]
        packages = [p for p in roots if (p / "pyproject.toml").is_file()]
        roots = packages or roots
        if not roots:
            raise UpdateError("bad-archive", "源码包里没有目录", EXIT_INSTALL)
        src = roots[0]

        # A tag without a matching version bump would install code that still
        # reports the old version, so the daemon restarts, sees the update
        # again and loops forever. Fail before pip touches the venv.
        declared = _source_version(src)
        expected = ".".join(str(part) for part in parse_version(tag))
        if declared and expected and compare_versions(declared, expected) != 0:
            raise UpdateError(
                "version-mismatch",
                f"tag {tag} 的源码包版本是 {declared}，拒绝安装（会导致更新循环）",
                EXIT_INSTALL)

        uv = _find_uv()
        if uv:
            cmd = [uv, "pip", "install", "--python", sys.executable,
                   "--reinstall", str(src)]
        else:
            # uv 建的 venv 默认没有 pip：先补一个再说
            # uv/pip spew UTF-8 (paths may contain non-ASCII: 用户目录、中文路径);
            # text=True 会按 Windows 本地代码页解码并在更新自检里抛 UnicodeDecodeError。
            subprocess.run([sys.executable, "-m", "ensurepip", "--upgrade"],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
            cmd = [sys.executable, "-m", "pip", "install", "--force-reinstall",
                   "--no-input", str(src)]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=600)
        if proc.returncode != 0:
            raise UpdateError("install-failed", (proc.stderr or proc.stdout)[-400:])
        return tag.lstrip("vV")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def shutil_which(name: str) -> str | None:
    import shutil
    return shutil.which(name)


def _find_uv() -> str | None:
    """systemd 服务的 PATH 很短，uv 常在用户目录里，得自己找。"""
    import shutil
    found = shutil.which("uv")
    if found:
        return found
    for candidate in ("/root/.local/bin/uv", "/usr/local/bin/uv",
                      str(Path.home() / ".local/bin/uv")):
        if Path(candidate).exists():
            return candidate
    return None
