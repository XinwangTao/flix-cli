#!/usr/bin/env python3
"""Render the desktop UI offscreen to PNG files for design review.

Usage:  python scripts/ui_shots.py [out_dir]
Requires the GUI extra (PySide6). No daemon or network is needed: a demo
payload is injected, so the shots are deterministic and safe to diff.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# The offscreen platform enumerates no system fonts on this host, so Chinese
# renders as tofu. Use the real Windows platform and park the window far off
# screen instead; grab() still works and font fallback is correct.
os.environ.pop("QT_QPA_PLATFORM", None)

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"D:\UserData\Temp\flix-ui")

sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtWidgets import QApplication

from flixcli import gui

APP = QApplication.instance() or QApplication([])
NOW = int(time.time() * 1000)

ME = "3f9c2a8e-6d14-4b7a-9c2e-51a0b7d4e111"
PEER = "8b1d5c7a-2e93-4f60-a8d1-77c4e9b2a222"
TABLET = "c4e7a1d9-5b28-4c03-9f7a-2d6e8b1c3333"
PHONE = "d9a3f6b2-7c41-4e85-b0d3-6a2f9c4e4444"
GROUP = "f1b8d4c6-3a72-4e09-8c5b-9d7a2e6f5555"


def payload() -> dict:
    devices = [
        {"fingerprint": PEER, "alias": "设计工作站", "os": "windows",
         "ip": "10.37.73.198", "port": 8891, "version": "2.2.1",
         "seenAt": NOW - 4_000},
        {"fingerprint": TABLET, "alias": "车间平板", "os": "android",
         "ip": "10.37.73.94", "port": 8891, "version": "2.4.0+127",
         "seenAt": NOW - 75_000},
        {"fingerprint": PHONE, "alias": "仓库手机", "os": "android",
         "ip": "10.37.73.51", "port": 8891, "version": "2.4.0+127",
         "seenAt": NOW - 900_000},
    ]
    # store.bubbles() returns newest first; the UI reverses it.
    bubbles = [
        {"id": "b6b", "from": PEER, "to": ME, "type": 3, "time": NOW - 45_000,
         "groupId": "",
         "content": {"name": "飞达3号线_换线记录_2026-11.pdf",
                     "size": 7_340_032, "resourceId": "res-2",
                     "state": 1, "progress": 0.62}},
        {"id": "b6", "from": PEER, "to": ME, "type": 0, "time": NOW - 60_000,
         "groupId": "", "content": "OK，辛苦。"},
        {"id": "b5", "from": ME, "to": PEER, "type": 0, "time": NOW - 120_000,
         "groupId": "", "content": "收到了，SHA256 对得上。下午 3 点前给你确认。"},
        {"id": "b4", "from": PEER, "to": ME, "type": 3, "time": NOW - 3_420_000,
         "groupId": "",
         "content": {"name": "飞达3号线_贴片坐标_v12.xlsx",
                     "mimeType": "application/vnd.ms-excel",
                     "size": 18_874_368, "resourceId": "res-1",
                     "state": 2, "progress": 1}},
        {"id": "b3", "from": PEER, "to": ME, "type": 0, "time": NOW - 3_480_000,
         "groupId": "", "content": "好，文件有点大，大概 18 MB。"},
        {"id": "b2", "from": ME, "to": PEER, "type": 0, "time": NOW - 3_540_000,
         "groupId": "", "content": "收到，先发我一份最新的贴片坐标，我核对一下物料。"},
        {"id": "b1", "from": PEER, "to": ME, "type": 0, "time": NOW - 3_600_000,
         "groupId": "", "content": "早上好，飞达 3 号线的程序我已经改完了，麻烦帮忙看一下。"},
        {"id": "g2", "from": PHONE, "to": GROUP, "type": 0,
         "time": NOW - 2_300_000, "groupId": GROUP,
         "content": "仓库这边备料已齐，换线前可以领用。"},
        {"id": "g1", "from": TABLET, "to": GROUP, "type": 0,
         "time": NOW - 2_400_000, "groupId": GROUP,
         "content": "3 号线今天下午换线，计划 14:30 开始。"},
    ]
    return {
        "self": {"id": ME, "alias": "jlc 工作站", "port": 18991},
        "devices": devices,
        "groups": [
            {"id": GROUP, "name": "飞达 3 号线", "members": [ME, TABLET, PHONE, PEER],
             "rev": 7},
        ],
        "bubbles": bubbles,
        "adb": {"devices": [
            {"serial": "PKJ110-3f21", "model": "OPPO PKJ110", "state": "device",
             "authorized": True},
            {"serial": "10.37.73.94:5555", "model": "车间平板", "state": "device",
             "authorized": True},
        ]},
        "settings": {"saveDir": r"D:\feeder-export\downloads"},
        "media": {"sessions": []},
    }


def shot(window, name: str, theme: str) -> None:
    window.apply_theme(theme)
    window.resize(1280, 840)
    APP.processEvents()
    window.grab().save(str(OUT / f"{name}_{theme}.png"))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    window = gui.MainWindow(19999)
    window.timer.stop()
    # The constructor's probe would race the loop and flip the page offline.
    if window.worker is not None:
        window.worker.blockSignals(True)
    window.payload = payload()
    window.self_id = ME
    window.apply_overview(window.payload)
    window.offline_mode = False
    window._sync_signal()
    window.move(-5000, -5000)  # park it; fonts still enumerate on this platform
    window.show()
    APP.processEvents()

    for theme in ("light", "dark"):
        window.set_mode("devices")
        window.current = {"kind": "device", "key": PEER, "title": "设计工作站"}
        window.render_current()
        window.populate_sidebar()
        APP.processEvents()
        shot(window, "chat", theme)

        window.set_mode("groups")
        window.current = {"kind": "group", "key": GROUP, "title": "飞达 3 号线"}
        window.render_current()
        window.populate_sidebar()
        APP.processEvents()
        shot(window, "group", theme)

        for mode in ("transfers", "adb", "settings"):
            window.set_mode(mode)
            APP.processEvents()
            shot(window, mode, theme)

        window.current = None
        window.set_mode("devices")
        APP.processEvents()
        shot(window, "empty", theme)

        window.offline_mode = True
        window._sync_sidebar_for_mode()
        window.populate_sidebar()  # cached dots must read 未知, not 在线
        window._sync_header()
        window._sync_signal()
        window.entity_list.clear()
        window.sidebar_footer.setText("最后已知 · jlc 工作站 · 通道 18991")
        window.workspace_stack.setCurrentIndex(6)
        APP.processEvents()
        shot(window, "offline", theme)
        window.offline_mode = False
        window.apply_overview(window.payload)

    print(json.dumps({"out": str(OUT), "files": sorted(p.name for p in OUT.glob("*.png"))},
                     ensure_ascii=False))
    window.timer.stop()
    window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
