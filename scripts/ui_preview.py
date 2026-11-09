#!/usr/bin/env python3
"""Render RelayDesk to PNGs so a UI change can be reviewed without a desktop.

The GUI is a QSS-styled Qt app; every visual regression (a token that stopped
resolving, a page that keeps the old palette, a bubble that collapsed) shows up
in a screenshot long before it shows up in a unit test. This script drives the
real ``MainWindow`` off-screen with a fixed fake payload, so two revisions
produce comparable images.

    python scripts/ui_preview.py --out /tmp/ui

Output: chat-{dark,light}.png, settings-{dark,light}.png, plus narrow shots
at 1100 px for the sidebar breakpoint.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# The offscreen platform enumerates no system fonts on this host, so Chinese
# renders as tofu. Use the real platform where the OS has one.
if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
else:
    os.environ.pop("QT_QPA_PLATFORM", None)

ME = "fp-me-aaaaaaaaaaaa"
PEER = "fp-peer-111111111111"
PEER2 = "fp-peer-222222222222"
NOW = 1_740_000_000_000


def demo_payload() -> dict:
    bubbles = []

    def add(sender: str, text: str, offset: int, kind: int = 0,
            content=None) -> None:
        bubbles.append({
            "id": f"b{len(bubbles)}", "from": sender,
            "to": PEER if sender == ME else ME, "type": kind,
            "time": NOW + offset, "groupId": "",
            "content": text if content is None else content,
        })

    add(PEER, "在吗？昨天那个 relay 的改动合并了吗", 0)
    add(ME, "合并了，刚推上去。", 60_000)
    add(PEER, "好，我这边拉一下。顺便问一下，群组同步的边界情况你测过没有？", 120_000)
    add(ME, "测过了，包括成员离线、重复投递和乱序到达三种情况。", 180_000)
    # Same side, same type, inside the grouping window: these two must render
    # as one group, which only a screenshot proves.
    add(ME, "日志在这里：", 186_000)
    add(ME, "", 190_000, kind=1,
        content={"name": "relay-2025-02-20.log", "size": 48_231,
                 "path": "/nonexistent/relay-2025-02-20.log"})
    add(PEER, "👍 那我看一下今天的构建产物。", 240_000)
    add(ME, "dist 里有两个，macOS 那个还在跑。", 300_000)
    # The store hands the transcript over newest-first and the renderer flips
    # it; keep the sample honest or the preview shows the conversation backwards.
    bubbles.reverse()
    return {
        "self": {"id": ME, "alias": "工作站", "port": 9012},
        "devices": [
            {"fingerprint": PEER, "alias": "设计师的一加", "os": "Android 14",
             "ip": "192.168.1.23", "port": 9012, "lastSeen": NOW},
            {"fingerprint": PEER2, "alias": "MacBook Air", "os": "macOS 15",
             "ip": "192.168.1.31", "port": 9012, "lastSeen": NOW},
            {"fingerprint": "fp-peer-333", "alias": "旧平板", "os": "Android 11",
             "ip": "", "port": 0},
        ],
        "groups": [{"id": "g1", "name": "传输协议讨论", "members": [PEER, PEER2]}],
        "bubbles": bubbles,
        "adb": {"devices": [{"model": "Pixel 7", "state": "device",
                             "serial": "3A991FDH200", "authorized": True}]},
        "settings": {"saveDir": "C:/Users/User/Downloads"},
        "media": {"sessions": []},
    }


def wallpaper():
    """A stand-in desktop so the frosted material is visible in the shots."""
    from PySide6.QtCore import QPoint, QRect, Qt
    from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter

    image = QImage(1920, 1200, QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    gradient = QLinearGradient(0, 0, image.width(), image.height())
    gradient.setColorAt(0.0, QColor("#1b2233"))
    gradient.setColorAt(0.45, QColor("#26384a"))
    gradient.setColorAt(1.0, QColor("#141a26"))
    painter.fillRect(image.rect(), gradient)
    for x, y, radius, color in ((260, 240, 420, "#3f6fa8"),
                                (1500, 300, 380, "#8a5b7a"),
                                (900, 1050, 460, "#2f7d68")):
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(color))
        painter.drawEllipse(QRect(x - radius // 2, y - radius // 2, radius, radius))
    painter.end()
    return image, QPoint(0, 0)


def build(app, theme: str, size=(1280, 840), *, snap=None):
    """A window that never talks to a daemon and never reshuffles itself."""
    from flixcli import gui

    gui.LocalApi.discover = staticmethod(lambda port=0: None)
    gui.MainWindow.start_local_daemon = lambda self: None
    gui.MainWindow._discovery_done = lambda self, found: None
    gui.MainWindow._load_theme = lambda self: theme
    window = gui.MainWindow(19999, snapshot=snap)
    window.timer.stop()
    window.offline_mode = True
    window._auto_start_tried = True
    window.payload = demo_payload()
    window.self_id = ME
    window.current = {"kind": "device", "key": PEER}
    window._timeline_signature = None
    window.apply_theme(theme)
    window.resize(*size)
    window.show()
    window.populate_sidebar()
    window.set_mode("devices")
    window.render_current()
    return window


def settle(app, ticks: int = 8) -> None:
    for _ in range(ticks):
        app.processEvents()
        time.sleep(0.04)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="build/ui-preview",
                        help="directory for the PNGs")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=840)
    args = parser.parse_args(argv)

    from PySide6.QtWidgets import QApplication

    from flixcli import gui

    app = QApplication.instance() or QApplication([])
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    # Shots must not rewrite the user's real theme/material preferences.
    gui.PREFS_FILE = Path(tempfile.gettempdir()) / "flixcli-preview-prefs.json"
    snap = wallpaper()
    for theme in ("dark", "light"):
        window = build(app, theme, (args.width, args.height), snap=snap)
        settle(app)
        window.grab().save(str(out / f"chat-{theme}.png"))
        window.set_mode("settings")
        settle(app)
        window.grab().save(str(out / f"settings-{theme}.png"))
        window.close()
        settle(app, 2)
    for theme in ("dark", "light"):
        window = build(app, theme, (1100, 760), snap=snap)
        settle(app)
        window.grab().save(str(out / f"chat-narrow-{theme}.png"))
        window.close()
        settle(app, 2)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
