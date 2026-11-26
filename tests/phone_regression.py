"""手机侧自动回归：反复用 rdProbe 跑「拉模型 + 真实补全」，统计成功率。

HyperOS 禁止 adb 注入输入事件（INJECT_EVENTS），所以走 App 自带的 rdProbe
深链（只读）。这是唯一能在真机上反复验「拉模型 + 补全」的办法。

前置：手机已装 com.relaydesk.app.debug、已配对、adb 在线。
用法: python tests/phone_regression.py [轮数] [间隔秒]
"""
from __future__ import annotations

import re
import subprocess
import sys
import time

SERIAL = "c122c21c"
PKG = "com.relaydesk.app.debug"
ACT = f"{PKG}/com.relaydesk.app.MainActivity"

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
INTERVAL = int(sys.argv[2]) if len(sys.argv) > 2 else 20


def adb(*args: str, timeout: int = 180) -> str:
    out = subprocess.run(["adb", "-s", SERIAL, *args],
                         capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=timeout)
    return (out.stdout or "") + (out.stderr or "")


def run_probe() -> tuple[bool, str, str]:
    adb("logcat", "-c")
    adb("shell", "am", "force-stop", PKG)
    time.sleep(1.5)
    adb("shell", "am", "start", "-n", ACT, "-e", "rdProbe", "gateway")
    # 补全可能慢（上游 1.5–27s 波动），给足时间
    deadline = time.time() + 120
    models_line = ""
    completion_line = ""
    while time.time() < deadline:
        time.sleep(3)
        log = adb("logcat", "-d", "-s", "RelayDeskProbe")
        for line in log.splitlines():
            if "models(" in line and not models_line:
                models_line = line.strip()
            if "completion:" in line:
                completion_line = line.strip()
        if completion_line:
            break
    ok = "补全成功" in completion_line and "models(" in models_line
    count = ""
    match = re.search(r"models\((\d+)\)", models_line)
    if match:
        count = match.group(1)
    return ok, count, completion_line or models_line or "（无日志）"


def main() -> int:
    results = []
    print(f"手机侧回归 {ROUNDS} 轮，间隔 {INTERVAL}s", flush=True)
    for i in range(ROUNDS):
        ok, count, detail = run_probe()
        results.append(ok)
        print(f"  round {i + 1}: {'PASS' if ok else 'FAIL'} 模型={count or '?'} "
              f"| {detail[:110]}", flush=True)
        if i + 1 < ROUNDS:
            time.sleep(INTERVAL)
    passed = sum(results)
    print(f"\n{passed}/{ROUNDS} 通过")
    return 0 if passed == ROUNDS else 1


if __name__ == "__main__":
    raise SystemExit(main())
