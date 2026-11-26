"""手机 APK 是否落后于 Kotlin 源码？

姊妹检查：`check_dist_fresh.py` 守「exe 落后于 src」，这个守「APK 落后于 android/」。
两者是同一类错误——改了代码忘了重建，然后拿旧包做验证，结论「全过」但验的不是新代码。

判据：APK 的构建时间必须晚于最后一次 `android/app/src` 的提交。
没有 APK（比如干净的 clone）时跳过，不算失败——CI 会构建。
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APK = ROOT / "android" / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"


def git(*args: str) -> str:
    out = subprocess.run(["git", "-C", str(ROOT), *args],
                         capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    return (out.stdout or "").strip()


def main() -> int:
    if not APK.exists():
        print(f"没有本地 APK（{APK.relative_to(ROOT)}）—— 跳过；CI 会构建")
        return 0

    built = datetime.fromtimestamp(APK.stat().st_mtime)
    print(f"APK 构建时间: {built:%Y-%m-%d %H:%M}  "
          f"({APK.stat().st_size / 1024 / 1024:.1f} MB)")

    last = git("log", "-1", "--format=%cI", "--", "android/app/src")
    if not last:
        print("拿不到 android/app/src 的提交时间，跳过时间比对")
        return 0
    when = datetime.fromisoformat(last).replace(tzinfo=None)
    print(f"最后一次 android 源码提交: {when:%Y-%m-%d %H:%M}")
    if built < when:
        print("FAIL APK 早于最后一次 Kotlin 源码改动 —— 先重新构建 APK 再测手机")
        return 1
    print("OK   APK 晚于最后一次 Kotlin 源码改动")

    # 顺手报一下手机上装的是不是同一个（adb 不在就算了）
    try:
        out = subprocess.run(
            ["adb", "-s", "c122c21c", "shell",
             "dumpsys package com.relaydesk.app.debug | grep lastUpdateTime"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30)
        line = (out.stdout or "").strip()
        if line:
            print(f"手机上的安装时间: {line.split('=')[-1].strip()}")
    except (OSError, subprocess.SubprocessError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
