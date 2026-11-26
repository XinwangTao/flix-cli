"""级联自愈：源站（.7）网关重启期间，消费方（.200）会怎样？

真实现场：升级/看门狗重启源站网关，对端应该「短暂报错 → 自动恢复」，
不需要人在对端重启任何东西。

步骤：
  1. 确认对端当前可用；
  2. 重启源站网关（kill + start，模拟换包）；
  3. 期间每隔 2 秒问一次对端，记录中断窗口；
  4. 记录对端恢复所需时间，以及恢复后是否真的能补全。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(r"F:\projects\flix-cli-wt-live-toggle")
EXE = ROOT / "dist" / "RelayDesk.exe"
PEER = os.environ.get("RD_PEER_URL", "http://192.168.237.200:18993")
PEER_TOKEN = os.environ.get("RD_PEER_TOKEN", "")
if not PEER_TOKEN:
    raise SystemExit("请设置环境变量 RD_PEER_TOKEN（对端桌面端「复制手机配置」可拿）")


def models() -> tuple[int, int]:
    """返回 (模型数, HTTP 状态)；连不上时模型数记 -1。"""
    request = urllib.request.Request(PEER + "/v1/models",
                                     headers={"x-api-key": PEER_TOKEN})
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            body = json.loads(resp.read())
        return len(body.get("data") or []), resp.status
    except urllib.error.HTTPError as exc:
        return -1, exc.code
    except Exception:  # noqa: BLE001
        return -1, 0


def complete() -> bool:
    body = json.dumps({"model": "glm-5.3-flash", "max_tokens": 16,
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    request = urllib.request.Request(
        PEER + "/v1/messages", data=body,
        headers={"x-api-key": PEER_TOKEN, "content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=120) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    count, status = models()
    print(f"1) 对端初始: {count} 个模型，HTTP {status}", flush=True)
    if count <= 0:
        print("   对端当前就不可用，先别测")
        return 1

    # 重启源站网关
    print("2) 重启源站网关（模拟换包）…", flush=True)
    subprocess.run(["taskkill", "/IM", "RelayDesk.exe", "/F"],
                   capture_output=True)
    time.sleep(3)
    subprocess.Popen(
        [str(EXE), "serve", "--device-port", "18991", "--api-port", "18998"],
        cwd=str(EXE.parent), creationflags=subprocess.CREATE_NO_WINDOW)

    began = time.time()
    seen_bad = False
    recovered_at = None
    for i in range(60):
        time.sleep(2)
        count, status = models()
        elapsed = time.time() - began
        if count <= 0:
            seen_bad = True
            if i < 6 or i % 5 == 0:
                print(f"   t={elapsed:5.1f}s 对端无模型（HTTP {status}）", flush=True)
        elif seen_bad and recovered_at is None:
            recovered_at = elapsed
            print(f"   t={elapsed:5.1f}s 对端已恢复：{count} 个模型", flush=True)
            break
        elif not seen_bad and i == 0:
            print(f"   t={elapsed:5.1f}s 源站重启期间对端仍可用（缓存兜底）",
                  flush=True)

    print("3) 恢复后真的能补全吗…", flush=True)
    ok = False
    for _ in range(6):
        if complete():
            ok = True
            break
        time.sleep(5)
    print(f"   补全: {'成功' if ok else '失败'}", flush=True)

    print()
    print(f"中断窗口: {'观察到' if seen_bad else '未观察到（缓存覆盖了重启）'}")
    if recovered_at is not None:
        print(f"自动恢复用时: {recovered_at:.1f}s")
    print(f"结论: {'级联自愈正常' if (ok and (recovered_at or not seen_bad)) else '需要人看一眼'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
