"""dist 里的 exe 是不是当前源码构建的？

曾经的真实顺序问题：改完 src 直接跑验收，验的却是旧 exe（因为没重建），
结论「全过」但验的不是新代码。这比不验更糟——它给了虚假的安全感。

判据（两个都要看，任一不满足就算落后）：
  1. exe 的构建时间早于最后一次 src/ 提交；
  2. exe 自报的版本与 pyproject 不一致。

不检查「exe 与源码逐字节一致」（PyInstaller 不可复现，做不到），
只保证「构建发生在最后一次源码改动之后」。
"""
from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "dist" / "RelayDesk.exe"


def git(*args: str) -> str:
    out = subprocess.run(["git", "-C", str(ROOT), *args],
                         capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    return (out.stdout or "").strip()


def main() -> int:
    if not EXE.exists():
        print("dist/RelayDesk.exe 不存在 —— 先用 scripts/build_exe.ps1 或 PyInstaller 构建")
        return 1

    built = datetime.fromtimestamp(EXE.stat().st_mtime)
    print(f"exe 构建时间: {built:%Y-%m-%d %H:%M}  大小 {EXE.stat().st_size}")

    last_src = git("log", "-1", "--format=%cI", "--", "src")
    if not last_src:
        print("拿不到 src 的最后提交时间，跳过时间比对")
    else:
        when = datetime.fromisoformat(last_src)
        stale = built < when.replace(tzinfo=None)
        print(f"最后一次 src 提交: {when:%Y-%m-%d %H:%M}")
        if stale:
            print("FAIL exe 早于最后一次源码改动 —— 先重建再验收")
            return 1
        print("OK   构建晚于最后一次源码改动")

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'version\s*=\s*"([^"]+)"', pyproject)
    want = match.group(1) if match else ""
    out = subprocess.run([str(EXE), "--version"], capture_output=True,
                         text=True, encoding="utf-8", errors="replace",
                         timeout=60)
    reported = (out.stdout or "") + (out.stderr or "")
    print(f"exe 自报版本: {reported.strip().splitlines()[0] if reported.strip() else '(无输出)'}")
    print(f"pyproject:   {want}")
    if want and want not in reported:
        print("FAIL exe 自报版本与 pyproject 不一致")
        return 1
    print("OK   版本一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
