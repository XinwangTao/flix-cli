"""RelayDesk desktop client — dark, frameless, three-column.

Presentation layer targets a modern Windows 11 look: frameless window with a
custom title bar, native rounded corners, device avatars, layered surfaces.
All state comes from flixd's loopback API; functional behavior is unchanged.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
try:
    from ctypes import wintypes
except (ImportError, ValueError):  # macOS/Linux: ctypes.wintypes is Windows-only
    wintypes = None
from datetime import datetime
from pathlib import Path
from string import Template

from PySide6.QtCore import (
    QEasingCurve, QEvent, QPoint, QPropertyAnimation, QSize, QThread, QTimer, Qt, QUrl, Signal,
)
from PySide6.QtGui import (
    QAction, QColor, QDesktopServices, QFont, QPainter, QPalette, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QPushButton,
    QScrollArea, QSizePolicy, QStackedWidget, QStatusBar, QSystemTrayIcon, QTextEdit,
    QVBoxLayout, QWidget,
)

from .icons import icon, logo_icon, logo_pixmap
from .presence import is_online


if sys.platform == "darwin":
    FONT_FAMILIES = ["SF Pro Text", "Helvetica Neue", "PingFang SC"]
    FONT_STACK = '"SF Pro Text", "Helvetica Neue", "PingFang SC"'
else:
    FONT_FAMILIES = ["Segoe UI Variable Text", "Microsoft YaHei UI",
                     "Microsoft YaHei", "Segoe UI"]
    FONT_STACK = ('"Segoe UI Variable Text", "Microsoft YaHei UI", '
                  '"Microsoft YaHei", "Segoe UI"')


class ApiError(RuntimeError):
    pass


def pick_notification(bubbles: list[dict], previous_ids, self_id: str, *,
                      active: bool, enabled: bool) -> dict | None:
    """Which bubble deserves a toast, or None when we stay quiet.

    `previous_ids is None` means this is the very first poll: seed only.
    """
    if previous_ids is None or not enabled or active:
        return None
    fresh = [b for b in bubbles
             if b.get("id") not in previous_ids and b.get("from") != self_id]
    if not fresh:
        return None
    return max(fresh, key=lambda b: b.get("time") or 0)


def notification_text(bubble: dict, devices: list[dict]) -> tuple[str, str]:
    sender = bubble.get("from") or ""
    alias = next((d.get("alias") for d in devices
                  if d.get("fingerprint") == sender), "")
    alias = alias or sender[:8] or "新消息"
    content = bubble.get("content")
    if isinstance(content, dict):
        body = "▤ " + str(content.get("name") or "文件")
    else:
        body = str(content or "")
    body = " ".join(body.split())[:120] or "（空消息）"
    title = f"{alias} · 群消息" if bubble.get("groupId") else alias
    return title, body


TOAST_W, TOAST_H, TOAST_MARGIN, TOAST_GAP = 340, 86, 18, 10


def toast_position(area: tuple[int, int, int, int], index: int, *,
                   w: int = TOAST_W, h: int = TOAST_H,
                   margin: int = TOAST_MARGIN, gap: int = TOAST_GAP) -> tuple[int, int]:
    """Top-left corner of the index-th toast, stacked up from the corner.

    `area` is (x, y, width, height) of a screen's available geometry, so this
    stays testable without Qt.
    """
    ax, ay, aw, ah = area
    return (ax + aw - w - margin, ay + ah - h - margin - index * (h + gap))


class Toast(QWidget):
    """Bottom-right notification card drawn by the app itself.

    Windows quietly drops Shell_NotifyIcon balloons on plenty of machines (no
    AppUserModelID, notifications muted at the OS level), and on this box
    `QSystemTrayIcon.showMessage` produced nothing at all. Drawing the card
    ourselves makes the toast independent of shell settings.
    """

    closed = Signal()

    def __init__(self, title: str, body: str, *, dark: bool, on_click=None):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFixedSize(TOAST_W, TOAST_H)
        self._on_click = on_click
        self._closing = False
        bg = "rgba(22,29,26,0.97)" if dark else "rgba(255,255,255,0.98)"
        border = "rgba(255,255,255,0.10)" if dark else "rgba(27,34,30,0.10)"
        fg = "#e9eeeb" if dark else "#1b221e"
        sub = "#8a978f" if dark else "#5f6c66"
        accent = "#4fc38f" if dark else "#1f7a5c"

        card = QFrame(self)
        card.setObjectName("toastCard")
        card.setGeometry(0, 0, TOAST_W, TOAST_H)
        card.setStyleSheet(
            f"#toastCard {{ background: {bg}; border: 1px solid {border};"
            " border-radius: 12px; }")
        inner = QVBoxLayout(card)
        inner.setContentsMargins(16, 12, 16, 12)
        inner.setSpacing(4)
        head = QLabel(title)
        head.setStyleSheet(f"color: {accent}; font-size: 12px; font-weight: 600;")
        text = QLabel(body)
        text.setStyleSheet(f"color: {fg}; font-size: 13px;")
        text.setWordWrap(True)
        text.setMaximumHeight(40)
        hint = QLabel("点击打开 RelayDesk")
        hint.setStyleSheet(f"color: {sub}; font-size: 11px;")
        inner.addWidget(head)
        inner.addWidget(text, 1)
        inner.addWidget(hint)

        self._slide = QPropertyAnimation(self, b"pos", self)
        self._slide.setDuration(160)
        self._slide.setEasingCurve(QEasingCurve.OutCubic)

    def fade_in(self):
        """Show with a slide-in.

        Deliberately not a `windowOpacity` animation: on Windows that collides
        with `WA_TranslucentBackground` (SetLayeredWindowAttributes vs Qt's
        per-pixel alpha path) and the window ends up on screen but unpainted.
        """
        target = self.pos()
        self.move(target.x() + 24, target.y())
        self.show()
        self._slide.stop()
        self._slide.setStartValue(self.pos())
        self._slide.setEndValue(target)
        self._slide.start()

    def start_lifetime(self, msecs: int = 5000):
        QTimer.singleShot(msecs, self.dismiss)

    def dismiss(self):
        if self._closing:
            return
        self._closing = True
        out = QPropertyAnimation(self, b"pos", self)
        out.setDuration(140)
        out.setStartValue(self.pos())
        out.setEndValue(self.pos() + QPoint(24, 0))
        out.finished.connect(self.close)
        self._slide = out
        out.start()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._on_click is not None:
            self._on_click()
        self.dismiss()

    def closeEvent(self, event):
        super().closeEvent(event)
        self.closed.emit()


class LocalApi:
    """Loopback client. Port 8898 may legitimately be occupied by the OFFICIAL
    Flix client (its Desktop API answers there too), so callers must verify a
    port actually speaks flixcli (/api/overview) instead of trusting it."""

    CANDIDATE_PORTS = (8898, 18998, 19098, 19598, 18997, 18996)

    def __init__(self, port: int = 8898):
        self.base = f"http://127.0.0.1:{port}"

    @classmethod
    def discover(cls, first_port: int = 8898) -> "LocalApi | None":
        """Return an API handle for the first port serving a flixcli overview."""
        import os
        candidates = [first_port]
        env = os.environ.get("FLIX_API_PORT")
        if env and env.isdigit():
            candidates.insert(0, int(env))
        candidates += [p for p in cls.CANDIDATE_PORTS if p not in candidates]
        for port in candidates:
            api = cls(port)
            try:
                data = api.get("/api/overview")
                if isinstance(data, dict) and "self" in data and "devices" in data:
                    return api
            except ApiError:
                continue
        return None

    def get(self, path: str):
        return self._request(path)

    def post(self, path: str, payload: dict):
        return self._request(path, payload)

    def bytes(self, path: str) -> bytes:
        try:
            with urllib.request.urlopen(self.base + path, timeout=3) as response:
                return response.read()
        except (OSError, urllib.error.URLError) as exc:
            raise ApiError(str(exc)) from exc

    def _request(self, path: str, payload: dict | None = None):
        try:
            data = json.dumps(payload).encode("utf-8") if payload is not None else None
            req = urllib.request.Request(self.base + path, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8"))
            except (ValueError, OSError):
                body = {"error": f"http-{exc.code}"}
            raise ApiError(body.get("detail") or body.get("error", str(exc))) from exc
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ApiError(str(exc)) from exc


class ApiThread(QThread):
    result = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.result.emit(self.fn())
        except Exception as exc:  # worker boundary; surface the exact message
            self.failed.emit(str(exc))


def avatar_color(key: str) -> QColor:
    # Three ink tones from the product's neutral family. Variation comes from
    # lightness, not a rainbow wheel: the list stays one palette.
    tones = ("#33473e", "#42574d", "#2a3b33")
    return QColor(tones[sum(key.encode()) % len(tones)])


class Avatar(QLabel):
    """Circle with the first glyph of the display name (hue from the key).

    Rendered into a QPixmap once — no custom paintEvent, so the widget is
    safe to construct before show.
    """

    def __init__(self, text: str, key: str, size: int = 38):
        super().__init__()
        self._size = size
        self.setFixedSize(size, size)
        self.set_alignment(Qt.AlignCenter)
        self.set_identity(text, key)

    def set_alignment(self, alignment):
        self.setAlignment(alignment)

    def set_identity(self, text: str, key: str):
        size = self._size
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(avatar_color(key)))
        painter.drawEllipse(1, 1, size - 2, size - 2)
        painter.setPen(QColor("white"))
        font = QFont(self.font())
        font.setPointSizeF(max(10.0, size / 30.0))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(pm.rect(), Qt.AlignCenter, text[:1].upper())
        painter.end()
        self.setPixmap(pm)


class TitleBar(QFrame):
    """Custom title bar: drag, double-click maximize, window buttons."""

    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.window = window
        self.setFixedHeight(42)
        self.setObjectName("titleBar")
        self._icon_color = "#8fa39a"

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 0, 0, 0)
        row.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(logo_pixmap(24))
        name = QLabel("RelayDesk")
        name.setObjectName("titleName")
        row.addWidget(logo)
        row.addWidget(name)
        row.addStretch()

        self.btn_min = self._win_button("min", self._minimize)
        self.btn_min.setAccessibleName("最小化")
        self.btn_max = self._win_button("max", self._toggle_max)
        self.btn_max.setAccessibleName("最大化")
        self.btn_close = self._win_button("close", self._close, danger=True)
        self.btn_close.setAccessibleName("关闭")
        for b in (self.btn_min, self.btn_max, self.btn_close):
            row.addWidget(b)

    def _win_button(self, icon_name: str, fn, danger: bool = False) -> QPushButton:
        b = QPushButton()
        b.setObjectName("closeBtn" if danger else "winBtn")
        b.setIcon(icon(icon_name, self._icon_color, 15))
        b.setIconSize(QSize(15, 15))
        b.setFixedSize(44, 42)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(fn)
        return b

    def refresh_icons(self, color: str) -> None:
        """Window buttons are pixmaps, so a theme switch must redraw them."""
        self._icon_color = color
        self.btn_min.setIcon(icon("min", color, 15))
        self.btn_max.setIcon(
            icon("restore" if self.window.isMaximized() else "max", color, 15))
        self.btn_close.setIcon(icon("close", color, 15))

    def _minimize(self):
        self.window.showMinimized()

    def _toggle_max(self):
        if self.window.isMaximized():
            self.window.showNormal()
            self.btn_max.setIcon(icon("max", self._icon_color, 15))
        else:
            self.window.showMaximized()
            self.btn_max.setIcon(icon("restore", self._icon_color, 15))

    def _close(self):
        self.window.close()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.window._drag_pos = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event):
        if self.window._drag_pos is not None and not self.window.isMaximized():
            delta = event.globalPosition().toPoint() - self.window._drag_pos
            self.window.move(self.window.pos() + delta)
            self.window._drag_pos = event.globalPosition().toPoint()

    def mouseReleaseEvent(self, event):
        self.window._drag_pos = None

    def mouseDoubleClickEvent(self, event):
        self._toggle_max()


class MainWindow(QMainWindow):
    HOT_MARGIN = 6

    def __init__(self, api_port: int = 8898):
        super().__init__()
        self.api = LocalApi(api_port)
        self.api_port = api_port
        self.worker: ApiThread | None = None
        self.action_worker: ApiThread | None = None
        self.payload: dict = {}
        self.self_id = ""
        self.current: dict | None = None
        self.media_seq = 0
        self._drag_pos: QPoint | None = None
        self.offline_mode = False
        self._auto_start_tried = False
        self._seen_bubbles: set[str] | None = None
        self._tray = None
        self._toasts: list[Toast] = []
        # Rebuilding the transcript every poll destroys any text selection, so
        # the renderer remembers what it last drew and skips identical frames.
        self._timeline_signature = None
        # "Follow the tail": true while the reader is at the newest message.
        # Switching conversations turns it on; scrolling up turns it off so a
        # poll cannot drag the reader away from history.
        self._follow_tail = True
        self._timeline_rebuilding = False
        # Mounted transcript rows, keyed by message id (days get their own key).
        # The renderer diffs against these instead of rebuilding everything.
        self._timeline_keys = []
        self._timeline_rows = {}
        self._timeline_row_sig = {}
        self._anchor_target = None
        self._pixmap_cache = {}
        self._render_seq = 0
        # True only when the reader moved the timeline themselves; layout
        # reflows and programmatic restores must not count as user intent.
        self._user_scrolled = False
        self.setWindowTitle("RelayDesk")
        self.resize(1280, 840)
        self.setMinimumSize(980, 640)
        if sys.platform == "darwin":
            # macOS keeps the native title bar: traffic lights, resize borders
            # and window dragging come from the system, not from WM_NCHITTEST.
            self.setWindowFlags(Qt.Window)
        else:
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setObjectName("rootWindow")
        self._build()
        self._enable_round_corners()
        self.refresh()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2500)

    def showEvent(self, event):
        super().showEvent(event)
        # Self-heal: frameless windows shown on a busy desktop can end up with
        # a collapsed geometry; re-assert the minimum and restore.
        self.setMinimumSize(980, 640)
        if self.width() < 980 or self.height() < 640:
            self.resize(1280, 840)

    def closeEvent(self, event):
        self.timer.stop()
        self.offline_mode = True  # stop the refresh/discovery churn
        for worker in (self.worker, self.action_worker,
                       getattr(self, "_discovery_worker", None)):
            if worker is not None:
                try:
                    worker.quit()
                    worker.wait(1500)
                except Exception:
                    pass
        super().closeEvent(event)

    # ---------- frameless plumbing ----------

    def _enable_round_corners(self):
        if sys.platform == "win32":
            try:
                hwnd = int(self.winId())
                pref = ctypes.c_int(2)  # DWMWCP_ROUND
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, 33, ctypes.byref(pref), 4)
            except Exception:
                pass

    def nativeEvent(self, eventType, message):
        # WM_NCHITTEST: native resize borders + aero snap for the frameless window
        if eventType == "windows_generic_MSG" and sys.platform == "win32":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0084:  # WM_NCHITTEST
                x = ctypes.c_short(msg.lParam & 0xFFFF).value
                y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
                rect = self.geometry()
                g = self.mapToGlobal(QPoint(0, 0))
                left, top = g.x(), g.y()
                right, bottom = left + rect.width(), top + rect.height()
                m = self.HOT_MARGIN
                on_left, on_right = x - left < m, right - x < m
                on_top, on_bottom = y - top < m, bottom - y < m
                if on_top and on_left:
                    return True, 13  # HTTOPLEFT
                if on_top and on_right:
                    return True, 14
                if on_bottom and on_left:
                    return True, 16
                if on_bottom and on_right:
                    return True, 17
                if on_left:
                    return True, 10
                if on_right:
                    return True, 11
                if on_top:
                    return True, 12
                if on_bottom:
                    return True, 15
        return super().nativeEvent(eventType, message)

    # ---------- construction ----------

    def _apply_app_font(self) -> None:
        """Native type per platform; Chinese falls back to the system CJK font."""
        app = QApplication.instance()
        if app is None:
            return
        font = app.font()
        font.setFamilies(FONT_FAMILIES)
        font.setPointSizeF(9.75)
        app.setFont(font)

    def _build(self):
        self._apply_app_font()
        self.theme = self._load_theme()
        central = QWidget()
        central.setObjectName("rootWindow")
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.title_bar = TitleBar(self)
        self.title_bar.setVisible(sys.platform != "darwin")
        outer.addWidget(self.title_bar)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_rail())
        body.addWidget(self._build_sidebar())
        body.addWidget(self._build_workspace(), 1)
        outer.addLayout(body, 1)
        self.setCentralWidget(central)
        status = QStatusBar()
        status.setSizeGripEnabled(False)
        self.setStatusBar(status)
        self.statusBar().showMessage("正在连接本机服务…")
        self.apply_theme(self.theme)

    def _build_rail(self):
        rail = QFrame()
        rail.setObjectName("rail")
        rail.setFixedWidth(64)
        box = QVBoxLayout(rail)
        box.setContentsMargins(10, 14, 10, 12)
        box.setSpacing(6)
        logo = QLabel()
        logo.setPixmap(logo_pixmap(36))
        box.addWidget(logo, 0, Qt.AlignHCenter)
        box.addSpacing(12)
        self.rail_buttons = []
        for name, tip, mode in (
            ("devices", "设备会话", "devices"), ("search", "搜索", "search"),
            ("groups", "群组", "groups"), ("transfers", "传输任务", "transfers"),
            ("android", "Android / ADB", "adb"), ("settings", "设置与状态", "settings"),
        ):
            button = QPushButton()
            button.setObjectName("railButton")
            button.setToolTip(tip)
            button.setAccessibleName(tip)
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setFixedSize(44, 44)
            button.setProperty("iconName", name)
            button.setIcon(icon(name, PALETTES[self.theme]["icon"], 20))
            button.setIconSize(QSize(20, 20))
            button.clicked.connect(lambda _checked=False, m=mode: self.set_mode(m))
            button.setProperty("mode", mode)
            button.toggled.connect(self._refresh_rail_icon)
            self.rail_buttons.append(button)
            box.addWidget(button, 0, Qt.AlignHCenter)
        box.addStretch()
        self.connection_dot = QLabel("●")
        self.connection_dot.setObjectName("connectionDot")
        self.connection_dot.setAlignment(Qt.AlignCenter)
        box.addWidget(self.connection_dot)
        return rail

    def _refresh_rail_icon(self, _checked: bool) -> None:
        colors = PALETTES[self.theme]
        for button in getattr(self, "rail_buttons", []):
            name = button.property("iconName")
            if name:
                button.setIcon(icon(
                    name, colors["accent"] if button.isChecked() else colors["icon"], 20))

    def _build_sidebar(self):
        panel = QFrame()
        panel.setObjectName("sidebar")
        panel.setFixedWidth(312)
        box = QVBoxLayout(panel)
        box.setContentsMargins(20, 20, 16, 14)
        box.setSpacing(12)
        title_row = QHBoxLayout()
        brand_box = QVBoxLayout()
        brand = QLabel("RelayDesk")
        brand.setObjectName("brand")
        brand_sub = QLabel("局域网设备协作中心")
        brand_sub.setObjectName("brandSub")
        brand_box.addWidget(brand)
        brand_box.addWidget(brand_sub)
        title_row.addLayout(brand_box)
        title_row.addStretch()
        refresh = QPushButton("↻")
        refresh.setObjectName("roundBtn")
        refresh.setToolTip("重新广播并刷新")
        refresh.setAccessibleName("刷新设备列表")
        refresh.setFixedSize(36, 36)
        refresh.setCursor(Qt.PointingHandCursor)
        refresh.clicked.connect(lambda: (self.refresh(), toast_hint(self, "已刷新")))
        add = QPushButton("＋")
        add.setObjectName("roundBtn")
        add.setToolTip("新建群组 / 添加设备")
        add.setAccessibleName("新建群组或添加设备")
        add.setFixedSize(36, 36)
        add.setCursor(Qt.PointingHandCursor)
        add.clicked.connect(self.show_add_menu)
        title_row.addWidget(refresh)
        title_row.addWidget(add)
        box.addLayout(title_row)
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("搜索设备、群组或消息")
        self.search.textChanged.connect(self.populate_sidebar)
        box.addWidget(self.search)
        self.sidebar_title = QLabel("设备")
        self.sidebar_title.setObjectName("sidebarTitle")
        box.addWidget(self.sidebar_title)
        self.entity_list = QListWidget()
        self.entity_list.setObjectName("entityList")
        self.entity_list.currentItemChanged.connect(self.select_entity)
        # Re-clicking the conversation already open should still take the
        # reader back to the newest message.
        self.entity_list.itemClicked.connect(self._on_entity_clicked)
        self.entity_list.setMouseTracking(True)
        box.addWidget(self.entity_list, 1)
        self.sidebar_footer = QLabel("等待服务状态")
        self.sidebar_footer.setObjectName("muted")
        box.addWidget(self.sidebar_footer)
        return panel

    def _build_workspace(self):
        area = QFrame()
        area.setObjectName("workspace")
        box = QVBoxLayout(area)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        box.addWidget(self._build_header())
        self.workspace_stack = QStackedWidget()
        self.workspace_stack.addWidget(self._build_empty())
        self.workspace_stack.addWidget(self._build_chat())
        self.workspace_stack.addWidget(self._build_transfers())
        self.workspace_stack.addWidget(self._build_adb())
        self.workspace_stack.addWidget(self._build_settings())
        self.workspace_stack.addWidget(self._build_remote())
        self.workspace_stack.addWidget(self._build_offline())
        box.addWidget(self.workspace_stack, 1)
        return area

    def _build_offline(self):
        page = QWidget()
        page.setObjectName("page")
        box = QVBoxLayout(page)
        box.addStretch()
        logo = QLabel()
        logo.setPixmap(logo_pixmap(88))
        logo.setAlignment(Qt.AlignCenter)
        wrap = QHBoxLayout()
        wrap.addStretch()
        wrap.addWidget(logo)
        wrap.addStretch()
        box.addLayout(wrap)
        box.addSpacing(16)
        self.offline_title = QLabel("未连接本机服务")
        self.offline_title.setObjectName("emptyTitle")
        self.offline_title.setAlignment(Qt.AlignCenter)
        box.addWidget(self.offline_title)
        self.offline_detail = QLabel(
            "默认端口 8898 可能被原生 Flix 占用。\n"
            "可一键启动本机服务（端口 18991/18998，与 Flix 共存）。")
        self.offline_detail.setObjectName("muted")
        self.offline_detail.setAlignment(Qt.AlignCenter)
        box.addWidget(self.offline_detail)
        box.addSpacing(18)
        actions = QHBoxLayout()
        self.start_daemon_button = QPushButton("启动本机服务")
        self.start_daemon_button.setObjectName("primaryBtn")
        self.start_daemon_button.setCursor(Qt.PointingHandCursor)
        self.start_daemon_button.clicked.connect(self.start_local_daemon)
        retry = QPushButton("重试连接")
        retry.setObjectName("secondaryButton")
        retry.setCursor(Qt.PointingHandCursor)
        retry.clicked.connect(self.retry_connection)
        wrap2 = QHBoxLayout()
        wrap2.addStretch()
        wrap2.addWidget(self.start_daemon_button)
        wrap2.addSpacing(10)
        wrap2.addWidget(retry)
        wrap2.addStretch()
        actions.addLayout(wrap2)
        box.addLayout(actions)
        box.addStretch()
        return page

    def start_local_daemon(self):
        """Start the background service detached (18991/18998)."""
        import subprocess
        from .config import APP_DIR
        log_dir = APP_DIR
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = open(log_dir / "flixd-gui-started.log", "ab")
        # One binary does both jobs: a frozen RelayDesk re-spawns *itself* with
        # `serve` (this is why launch_relaydesk.py routes arguments to the CLI).
        # Older builds needed a sibling flixd.exe, which is how users ended up
        # with a mismatched daemon.
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "serve"]
        else:
            cmd = [sys.executable, "-m", "flixcli.cli", "serve"]
        kwargs = {"stdout": log_file, "stderr": log_file, "close_fds": True}
        if sys.platform == "win32":
            kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                       | subprocess.CREATE_NO_WINDOW)
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(
            cmd + ["--device-port", "18991", "--api-port", "18998"], **kwargs)
        self.offline_detail.setText("正在启动本机服务（18991/18998）…几秒后自动重连。")
        self.start_daemon_button.setEnabled(False)
        QTimer.singleShot(3500, self.retry_connection)

    def retry_connection(self):
        found = LocalApi.discover(self.api_port)
        if found is None:
            self.offline_detail.setText("仍未发现 flixcli 服务。确认 flixd 已启动后重试。")
            self.start_daemon_button.setEnabled(True)
            return
        self.api = found
        self.api_port = found.base.rsplit(":", 1)[-1]
        self.api_port = int(self.api_port)
        self.offline_mode = False
        self.start_daemon_button.setEnabled(True)
        self.statusBar().showMessage(f"已连接本机服务 :{self.api_port}")
        self.refresh()

    def _build_header(self):
        header = QFrame()
        header.setObjectName("sessionHeader")
        header.setFixedHeight(72)
        row = QHBoxLayout(header)
        row.setContentsMargins(24, 10, 20, 10)
        row.setSpacing(12)
        self.session_avatar = Avatar("◉", "relaydesk", 40)
        row.addWidget(self.session_avatar)
        labels = QVBoxLayout()
        self.session_title = QLabel("选择设备或群组")
        self.session_title.setObjectName("sessionTitle")
        self.session_subtitle = QLabel("从左侧列表开始")
        self.session_subtitle.setObjectName("muted")
        labels.addSpacing(2)
        labels.addWidget(self.session_title)
        labels.addWidget(self.session_subtitle)
        row.addLayout(labels)
        row.addStretch()
        self.session_search = QLineEdit()
        self.session_search.setObjectName("sessionSearch")
        self.session_search.setPlaceholderText("在会话中搜索")
        self.session_search.setClearButtonEnabled(True)
        self.session_search.setFixedWidth(220)
        self.session_search.textChanged.connect(lambda _t: self.render_timeline())
        row.addWidget(self.session_search)
        self.header_actions = QHBoxLayout()
        self.header_actions.setSpacing(6)
        row.addLayout(self.header_actions)
        self._header_icon_buttons = []
        for glyph, tip, fn, icon_name in (
            ("▣", "发送文件", self.choose_files, "file"),
            ("⧉", "发送剪贴板文本", self.send_clipboard, "clipboard"),
            ("⌁", "请求远程观看", self.request_remote, "remote"),
            ("⋯", "更多操作", self.show_more, "more"),
        ):
            button = QPushButton()
            button.setObjectName("headerBtn")
            button.setToolTip(tip)
            button.setAccessibleName(tip)
            button.setFixedSize(36, 36)
            button.setIcon(icon(icon_name, PALETTES[self.theme]["icon"], 17))
            button.setIconSize(QSize(17, 17))
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(fn)
            self._header_icon_buttons.append((button, icon_name))
            self.header_actions.addWidget(button)
        return header

    def _build_empty(self):
        page = QWidget()
        page.setObjectName("page")
        box = QVBoxLayout(page)
        box.addStretch()
        halo = QLabel()
        halo.setPixmap(logo_pixmap(96))
        halo.setAlignment(Qt.AlignCenter)
        wrap = QHBoxLayout()
        wrap.addStretch()
        wrap.addWidget(halo)
        wrap.addStretch()
        box.addLayout(wrap)
        box.addSpacing(18)
        title = QLabel("选择一台设备开始协作")
        title.setObjectName("emptyTitle")
        title.setAlignment(Qt.AlignCenter)
        box.addWidget(title)
        note = QLabel("发送文本、图片、文件，或发起经过批准的远程会话")
        note.setObjectName("muted")
        note.setAlignment(Qt.AlignCenter)
        box.addWidget(note)
        box.addStretch()
        return page

    def _build_chat(self):
        page = QWidget()
        page.setObjectName("page")
        box = QVBoxLayout(page)
        box.setContentsMargins(24, 10, 24, 16)
        box.setSpacing(0)
        day_holder = QVBoxLayout()
        self.timeline = QScrollArea()
        self.timeline.setObjectName("timeline")
        self.timeline.setWidgetResizable(True)
        self.timeline_widget = QWidget()
        self.timeline_widget.setObjectName("timelineCanvas")
        self.timeline_layout = QVBoxLayout(self.timeline_widget)
        self.timeline_layout.setContentsMargins(10, 16, 10, 16)
        self.timeline_layout.setSpacing(10)
        self.timeline_layout.addStretch()
        self.timeline.setWidget(self.timeline_widget)
        # Right-clicking the empty space of the transcript still offers copy:
        # the bubbles cover most of it, but the gaps between them do not.
        self.timeline_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self.timeline_widget.customContextMenuRequested.connect(self._canvas_menu)
        # Ctrl+A/Ctrl+C are useless if the transcript keeps rebuilding under the
        # cursor, so the renderer skips unchanged frames and defers while a
        # selection is live. This is the safety net when nothing has focus.
        copy_all = QAction("复制整个会话", self)
        copy_all.setShortcut("Ctrl+Shift+C")
        copy_all.triggered.connect(self.copy_all_messages)
        self.addAction(copy_all)
        box.addWidget(self.timeline, 1)
        # 收到新消息但用户正在往上翻历史时，不抢滚动，只给这个提示。
        self.new_msg_btn = QPushButton("↓ 有新消息")
        self.new_msg_btn.setObjectName("newMsgPill")
        self.new_msg_btn.setCursor(Qt.PointingHandCursor)
        self.new_msg_btn.hide()
        self.new_msg_btn.clicked.connect(self.jump_to_latest)
        box.addWidget(self.new_msg_btn, 0, Qt.AlignHCenter)
        self.timeline.verticalScrollBar().valueChanged.connect(self._on_timeline_scroll)
        self.timeline.verticalScrollBar().rangeChanged.connect(
            self._on_timeline_range_changed)
        for widget in (self.timeline, self.timeline.viewport(),
                       self.timeline.verticalScrollBar()):
            widget.installEventFilter(self)
        composer = QFrame()
        composer.setObjectName("composer")
        cbox = QVBoxLayout(composer)
        cbox.setContentsMargins(14, 12, 14, 10)
        self.composer = QTextEdit()
        self.composer.setObjectName("composerEdit")
        self.composer.setPlaceholderText("输入消息，回车发送（Shift+回车换行）")
        self.composer.setMaximumHeight(96)
        self.composer.installEventFilter(self)
        cbox.addWidget(self.composer)
        actions = QHBoxLayout()
        actions.setSpacing(6)
        image = QPushButton()
        image.setObjectName("chipBtn")
        image.setToolTip("发送图片")
        image.setAccessibleName("发送图片")
        image.setFixedSize(36, 36)
        image.setIcon(icon("image", PALETTES[self.theme]["icon"], 16))
        image.setIconSize(QSize(16, 16))
        image.setCursor(Qt.PointingHandCursor)
        image.clicked.connect(lambda: self.choose_files(images_only=True))
        file_button = QPushButton()
        file_button.setObjectName("chipBtn")
        file_button.setToolTip("发送文件")
        file_button.setAccessibleName("发送文件")
        file_button.setFixedSize(36, 36)
        file_button.setIcon(icon("file", PALETTES[self.theme]["icon"], 16))
        file_button.setIconSize(QSize(16, 16))
        file_button.setCursor(Qt.PointingHandCursor)
        file_button.clicked.connect(self.choose_files)
        actions.addWidget(image)
        actions.addWidget(file_button)
        self._chip_icon_buttons = [(image, "image"), (file_button, "file")]
        self.composer_hint = QLabel("消息经本机服务发送")
        self.composer_hint.setObjectName("muted")
        actions.addSpacing(6)
        actions.addWidget(self.composer_hint)
        actions.addStretch()
        self.send_button = QPushButton(" 发送")
        self.send_button.setObjectName("sendButton")
        self.send_button.setIcon(icon("send", PALETTES[self.theme]["on_accent"], 15))
        self.send_button.setIconSize(QSize(15, 15))
        self.send_button.setCursor(Qt.PointingHandCursor)
        self.send_button.clicked.connect(self.send_message)
        actions.addWidget(self.send_button)
        cbox.addLayout(actions)
        box.addWidget(composer)
        return page

    def _build_transfers(self):
        page = QWidget(); page.setObjectName("page")
        box = QVBoxLayout(page); box.setContentsMargins(28, 24, 28, 24)
        title = QLabel("传输任务"); title.setObjectName("pageTitle"); box.addWidget(title)
        note = QLabel("文件气泡与落盘状态由 flixd 统一记录")
        note.setObjectName("muted"); box.addWidget(note); box.addSpacing(8)
        self.transfer_list = QListWidget(); self.transfer_list.setObjectName("contentList")
        box.addWidget(self.transfer_list, 1)
        return page

    def _build_adb(self):
        page = QWidget(); page.setObjectName("page")
        box = QVBoxLayout(page); box.setContentsMargins(28, 24, 28, 24)
        title = QLabel("Android / ADB"); title.setObjectName("pageTitle"); box.addWidget(title)
        note = QLabel("USB / 无线序列号与授权状态；文件操作要求显式目标。")
        note.setObjectName("muted"); box.addWidget(note); box.addSpacing(8)
        self.adb_list = QListWidget(); self.adb_list.setObjectName("contentList")
        box.addWidget(self.adb_list, 1)
        return page

    def _build_settings(self):
        page = QWidget(); page.setObjectName("page")
        box = QVBoxLayout(page); box.setContentsMargins(28, 24, 28, 24)
        title = QLabel("设置与状态"); title.setObjectName("pageTitle"); box.addWidget(title)
        box.addSpacing(6)
        card = QFrame(); card.setObjectName("settingsCard")
        card_box = QVBoxLayout(card); card_box.setContentsMargins(20, 18, 20, 18)
        self.settings_text = QLabel("等待服务配置")
        self.settings_text.setObjectName("settingsText")
        self.settings_text.setWordWrap(True)
        card_box.addWidget(self.settings_text)
        card_box.addSpacing(12)
        row = QHBoxLayout()
        self.copy_button = QPushButton("复制本机信息")
        self.copy_button.setObjectName("secondaryButton")
        self.copy_button.setCursor(Qt.PointingHandCursor)
        self.copy_button.clicked.connect(self.copy_settings_info)
        self.open_dir_button = QPushButton("打开下载目录")
        self.open_dir_button.setObjectName("secondaryButton")
        self.open_dir_button.setCursor(Qt.PointingHandCursor)
        self.open_dir_button.clicked.connect(self.open_save_dir)
        self.theme_button = QPushButton()
        self.theme_button.setObjectName("secondaryButton")
        self.theme_button.setCursor(Qt.PointingHandCursor)
        self.theme_button.clicked.connect(self.toggle_theme)
        row.addWidget(self.copy_button)
        row.addWidget(self.open_dir_button)
        row.addWidget(self.theme_button)
        row.addStretch()
        card_box.addLayout(row)
        self.notify_check = QCheckBox("桌面提醒：收到新消息时弹出提示")
        self.notify_check.setChecked(self._load_prefs().get("notify", True) is not False)
        self.notify_check.toggled.connect(self._toggle_notify)
        card_box.addWidget(self.notify_check)
        hint = QLabel("主题立即应用到整个窗口，下次启动沿用")
        hint.setObjectName("muted")
        card_box.addWidget(hint)
        box.addWidget(card)
        box.addStretch()
        self._sync_theme_button()
        return page

    def _load_prefs(self) -> dict:
        try:
            data = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_prefs(self, **changes):
        data = self._load_prefs()
        data.update(changes)
        try:
            PREFS_FILE.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass

    def _load_theme(self) -> str:
        return "light" if self._load_prefs().get("theme") == "light" else "dark"

    def apply_theme(self, theme: str):
        self.theme = "light" if theme == "light" else "dark"
        self.setStyleSheet(styles_for(self.theme))
        # Widgets the sheet does not reach (menus, scroll viewports, dialogs)
        # follow the palette, so flip it too or they keep the old theme.
        app = QApplication.instance()
        if app is not None:
            app.setPalette(palette_for(self.theme))
        # Stylesheet changes do not always repaint already-shown pages: the
        # stacked workspace keeps its old background until something touches it.
        for widget in self.findChildren(QWidget):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            # QListWidget/QListView override update(index); call the base one.
            QWidget.update(widget)
        self._refresh_icons()
        self._sync_theme_button()
        self.update()
        self.repaint()
        self._save_prefs(theme=self.theme)

    def _refresh_icons(self) -> None:
        """Icons are baked pixmaps: redraw them from the active palette."""
        colors = PALETTES[self.theme]
        self._refresh_rail_icon(False)
        for button, name in getattr(self, "_header_icon_buttons", []):
            button.setIcon(icon(name, colors["icon"], 17))
        for button, name in getattr(self, "_chip_icon_buttons", []):
            button.setIcon(icon(name, colors["icon"], 16))
        send = getattr(self, "send_button", None)
        if send is not None:
            send.setIcon(icon("send", colors["on_accent"], 15))
        title = getattr(self, "title_bar", None)
        if title is not None:
            title.refresh_icons(colors["muted"])

    def copy_settings_info(self):
        QApplication.clipboard().setText(self.settings_text.text())

    @staticmethod
    def default_save_dir() -> str:
        """Path.home() is a shim on this machine, so ask Windows first."""
        profile = os.environ.get("USERPROFILE") or str(Path.home())
        downloads = Path(profile) / "Downloads"
        if downloads.is_dir():
            return str(downloads)
        return profile if Path(profile).is_dir() else ""

    @staticmethod
    def _open_local(target: str) -> None:
        """Open a file or folder with the OS default app on every platform."""
        if sys.platform == "win32":
            os.startfile(target)
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    def open_save_dir(self):
        # The daemon only reports a saveDir when it was configured with one;
        # fall back to the OS Downloads folder the app writes to by default.
        configured = getattr(self, "save_dir", "")
        target = configured if configured and Path(configured).is_dir() else self.default_save_dir()
        if not target or not Path(target).is_dir():
            self.statusBar().showMessage(f"目录不存在：{target or '未设置'}", 5000)
            return
        try:
            self._open_local(target)
        except OSError as exc:
            self.statusBar().showMessage(f"打不开目录：{exc}", 4000)

    def _toggle_notify(self, on: bool):
        self._save_prefs(notify=bool(on))

    # ---------- notifications ----------

    def ensure_tray(self):
        if self._tray is not None:
            return self._tray
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        tray = QSystemTrayIcon(logo_icon(), self)
        tray.setToolTip("RelayDesk")
        menu = QMenu(self)
        menu.addAction("打开主窗口", self.restore_from_tray)
        menu.addAction("退出 RelayDesk", self.close)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda _reason: self.restore_from_tray())
        tray.show()
        self._tray = tray
        return tray

    def restore_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def window_is_attended(self) -> bool:
        """True only when the user is actually looking at the chat.

        `isActiveWindow()` alone is not enough: a window minimised by the shell
        can keep reporting itself as the active one, which silently swallowed
        every toast while the app sat in the taskbar.
        """
        return bool(self.isVisible()) and not self.isMinimized() \
            and self.isActiveWindow()

    def notify_new_bubbles(self, bubbles: list[dict]):
        """Toast for messages that arrived since the last poll.

        The first poll only seeds the id set: without that a fresh start would
        fire one toast per message already in the store.
        """
        seen = {b.get("id") for b in bubbles if b.get("id")}
        previous, self._seen_bubbles = self._seen_bubbles, seen
        chosen = pick_notification(
            bubbles, previous, self.self_id,
            active=self.window_is_attended(),
            enabled=self._load_prefs().get("notify", True) is not False)
        if chosen is not None:
            self.show_tray_message(chosen)

    def show_tray_message(self, bubble: dict):
        """Pop one notification card. The tray icon stays for menu/restore."""
        self.ensure_tray()
        title, body = notification_text(bubble, self.payload.get("devices") or [])
        while len(self._toasts) >= 3:  # never pile up more than three
            self._toasts[0].dismiss()
            self._drop_toast(self._toasts[0])
        toast = Toast(title, body, dark=self.theme == "dark",
                      on_click=self.restore_from_tray)
        toast.closed.connect(lambda t=toast: self._drop_toast(t))
        self._toasts.append(toast)
        self._place_toasts()
        toast.fade_in()
        toast.start_lifetime(5000)

    def _drop_toast(self, toast: Toast):
        if toast in self._toasts:
            self._toasts.remove(toast)
        self._place_toasts()

    def _place_toasts(self):
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        area = (geo.x(), geo.y(), geo.width(), geo.height())
        for index, toast in enumerate(self._toasts):
            toast.move(*toast_position(area, index))

    def toggle_theme(self):
        self.apply_theme("dark" if self.theme == "light" else "light")
        self._sync_theme_button()

    def _sync_theme_button(self):
        button = getattr(self, "theme_button", None)
        if button is not None:
            theme = getattr(self, "theme", "dark")
            button.setText("切换到浅色" if theme == "dark" else "切换到深色")

    def _build_remote(self):
        page = QWidget(); page.setObjectName("page")
        box = QVBoxLayout(page); box.setContentsMargins(24, 18, 24, 18)
        row = QHBoxLayout()
        self.remote_status = QLabel("默认仅观看；控制必须由对端单独批准")
        self.remote_status.setObjectName("muted")
        row.addWidget(self.remote_status)
        row.addStretch()
        self.remote_control = QCheckBox("请求控制")
        row.addWidget(self.remote_control)
        self.remote_request = QPushButton("请求观看")
        self.remote_request.setObjectName("primaryBtn")
        self.remote_request.setCursor(Qt.PointingHandCursor)
        self.remote_request.clicked.connect(self.request_remote)
        row.addWidget(self.remote_request)
        box.addLayout(row)
        box.addSpacing(8)
        self.remote_sessions = QListWidget(); self.remote_sessions.setObjectName("contentList")
        self.remote_sessions.currentItemChanged.connect(lambda _a, _b: self.update_remote_actions())
        box.addWidget(self.remote_sessions)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.approve_view = QPushButton("批准观看")
        self.approve_control = QPushButton("批准观看 + 控制")
        self.deny_remote = QPushButton("拒绝")
        self.connect_remote = QPushButton("连接")
        self.revoke_remote = QPushButton("断开")
        for button in (self.approve_view, self.approve_control, self.deny_remote,
                       self.connect_remote, self.revoke_remote):
            button.setObjectName("secondaryButton")
            button.setCursor(Qt.PointingHandCursor)
            actions.addWidget(button)
        self.approve_view.clicked.connect(lambda: self.decide_remote(["view"]))
        self.approve_control.clicked.connect(lambda: self.decide_remote(["view", "control"]))
        self.deny_remote.clicked.connect(self.deny_selected_remote)
        self.connect_remote.clicked.connect(self.connect_selected_remote)
        self.revoke_remote.clicked.connect(self.revoke_selected_remote)
        box.addLayout(actions)
        box.addSpacing(6)
        self.remote_preview = QLabel("等待远端画面")
        self.remote_preview.setObjectName("mediaPreview")
        self.remote_preview.setAlignment(Qt.AlignCenter)
        self.remote_preview.setMinimumHeight(320)
        self.remote_preview.installEventFilter(self)
        box.addWidget(self.remote_preview, 1)
        return page

    # ---------- navigation ----------

    def set_mode(self, mode: str):
        for button in self.rail_buttons:
            button.setChecked(button.property("mode") == mode)
        titles = {"devices": "设备", "search": "搜索", "groups": "群组",
                  "transfers": "传输", "adb": "Android / ADB", "settings": "设置"}
        self.sidebar_title.setText(titles.get(mode, "设备"))
        self.mode = mode
        if mode == "transfers":
            self.workspace_stack.setCurrentIndex(2)
        elif mode == "adb":
            self.workspace_stack.setCurrentIndex(3)
        elif mode == "settings":
            self.workspace_stack.setCurrentIndex(4)
        else:
            self.workspace_stack.setCurrentIndex(0 if self.current is None else 1)
        self.populate_sidebar()

    # ---------- data refresh ----------

    def refresh(self):
        if getattr(self, "offline_mode", False):
            return  # offline page waits for an explicit retry
        if self.worker and self.worker.isRunning():
            return
        self.worker = ApiThread(lambda: self.api.get("/api/overview"))
        self.worker.result.connect(self.apply_overview)
        self.worker.failed.connect(self.fail)
        self.worker.start()

    def apply_overview(self, payload: dict):
        self.payload = payload
        self.self_id = (payload.get("self") or {}).get("id", "")
        self.notify_new_bubbles(payload.get("bubbles") or [])
        self.connection_dot.setProperty("online", True)
        self.connection_dot.style().unpolish(self.connection_dot)
        self.connection_dot.style().polish(self.connection_dot)
        devices = payload.get("devices") or []
        adb = (payload.get("adb") or {}).get("devices", [])
        self.sidebar_footer.setText(
            f"{len(devices)} 台设备 · {len(payload.get('groups') or [])} 个群组 · {len(adb)} 条 ADB 连接")
        self.save_dir = (payload.get("settings") or {}).get("saveDir") or ""
        self.settings_text.setText(
            f"本机别名：{(payload.get('self') or {}).get('alias', '未设置')}\n"
            f"指纹：{self.self_id}\n"
            f"控制台端口：http://127.0.0.1:{self.api_port}\n"
            f"设备通道端口：{(payload.get('self') or {}).get('port', '未设置')}\n"
            f"下载目录：{(payload.get('settings') or {}).get('saveDir') or '系统 Downloads'}\n"
            f"活动媒体会话：{len((payload.get('media') or {}).get('sessions', []))}")
        self.populate_sidebar()
        self.populate_transfers()
        self.populate_adb()
        self.populate_remote()
        if self.current:
            self.render_current()
        self.statusBar().showMessage("最后更新 " + datetime.now().strftime("%H:%M:%S"))

    def populate_sidebar(self):
        if not hasattr(self, "entity_list"):
            return
        current_data = self.entity_list.currentItem().data(Qt.UserRole) if self.entity_list.currentItem() else None
        query = self.search.text().strip().lower()
        self.entity_list.blockSignals(True)
        self.entity_list.clear()
        mode = getattr(self, "mode", "devices")
        entities = []
        if mode in ("devices", "search"):
            for device in self.payload.get("devices") or []:
                title = device.get("alias") or device.get("fingerprint", "")[:8]
                subtitle = (f"{device.get('os') or 'unknown'} · "
                            f"{device.get('ip') or '离线'}:{device.get('port') or ''}")
                entities.append((title, subtitle, device.get("fingerprint", ""),
                                 "device", is_online(device)))
        if mode in ("groups", "search"):
            for group in self.payload.get("groups") or []:
                entities.append((group.get("name") or "未命名群组",
                                 f"{len(group.get('members', []))} 位成员 · 支持 @指纹",
                                 group.get("id", ""), "group", True))
        if mode == "adb":
            for device in (self.payload.get("adb") or {}).get("devices", []):
                entities.append((device.get("model") or "Android",
                                 f"{device.get('state')} · {device.get('serial')}",
                                 device.get("serial", ""), "adb",
                                 device.get("authorized", False)))
        for title, subtitle, key, kind, online in entities:
            if query and query not in (title + " " + subtitle + " " + key).lower():
                continue
            item = QListWidgetItem()
            item.setData(Qt.UserRole, {"key": key, "kind": kind, "title": title,
                                       "subtitle": subtitle, "online": online})
            item.setSizeHint(QSize(0, 64))
            self.entity_list.addItem(item)
            card = self._entity_card(title, subtitle, key, online)
            self.entity_list.setItemWidget(item, card)
            if current_data == item.data(Qt.UserRole):
                self.entity_list.setCurrentItem(item)
        self.entity_list.blockSignals(False)

    def _entity_card(self, title: str, subtitle: str, key: str, online: bool) -> QWidget:
        card = QFrame()
        card.setObjectName("entityCard")
        row = QHBoxLayout(card)
        row.setContentsMargins(10, 8, 10, 8)
        row.setSpacing(11)
        avatar = Avatar(title, key, 38)
        row.addWidget(avatar)
        col = QVBoxLayout()
        col.setSpacing(1)
        name = QLabel(title)
        name.setObjectName("entityName")
        sub = QLabel(subtitle)
        sub.setObjectName("entitySub")
        col.addWidget(name)
        col.addWidget(sub)
        row.addLayout(col, 1)
        dot = QLabel("●" if online else "○")
        dot.setObjectName("onlineDot" if online else "offlineDot")
        row.addWidget(dot)
        return card

    def select_entity(self, current, _previous):
        if current is None:
            return
        self.current = current.data(Qt.UserRole)
        self._follow_tail = True  # opening a conversation starts at the newest message
        self.render_current()

    def _on_entity_clicked(self, item):
        """Clicking the open conversation again jumps back to the newest message."""
        if item is None or not self.current:
            return
        data = item.data(Qt.UserRole) or {}
        if (data.get("kind"), data.get("key")) != (
                self.current.get("kind"), self.current.get("key")):
            return  # a different conversation: currentItemChanged already handles it
        self._follow_tail = True
        self.jump_to_latest()

    def render_current(self):
        if not self.current:
            self.workspace_stack.setCurrentIndex(0)
            return
        kind, key = self.current["kind"], self.current["key"]
        if kind == "adb":
            self.workspace_stack.setCurrentIndex(3)
            return
        self.workspace_stack.setCurrentIndex(1)
        if kind == "device":
            device = next((d for d in self.payload.get("devices", [])
                           if d.get("fingerprint") == key), {})
            title = device.get("alias") or key[:8]
            self.session_title.setText(title)
            self.session_subtitle.setText(
                f"{device.get('os') or 'unknown'} · {device.get('ip') or '离线'}:{device.get('port') or ''}")
            self.session_avatar.set_identity(title, key)
        else:
            group = next((g for g in self.payload.get("groups", []) if g.get("id") == key), {})
            title = group.get("name") or "群组"
            self.session_title.setText(title)
            self.session_subtitle.setText(f"{len(group.get('members', []))} 位成员 · 支持 @指纹前缀")
            self.session_avatar.set_identity("#", key)
        self.render_timeline()

    def render_timeline(self):
        if not self.current:
            self._timeline_signature = None
            self._clear_timeline()
            return
        kind, key = self.current["kind"], self.current["key"]
        query = getattr(self, "session_search", None)
        query = query.text().strip() if query is not None else ""
        messages = []
        for bubble in reversed(self.payload.get("bubbles") or []):
            if kind == "device" and key not in (bubble.get("from"), bubble.get("to")):
                continue
            if kind == "group" and bubble.get("groupId") != key:
                continue
            messages.append(bubble)
        if query:
            needle = query.lower()
            messages = [b for b in messages
                        if needle in self._search_text(b).lower()]
        items = self._timeline_items(messages, query)
        signature = (kind, key, query, tuple(item[3] for item in items))
        if signature == self._timeline_signature:
            return
        session_changed = getattr(self, "_timeline_session", None) != (kind, key)
        # A live selection must survive a poll, but it may not hold back a
        # conversation switch; pure appends are safe because no mounted widget
        # is touched.
        if (not session_changed and self.timeline_selection()
                and not self._is_safe_append(items)):
            QTimer.singleShot(1200, self.render_timeline)
            return
        bar = self.timeline.verticalScrollBar()
        saved_scroll = bar.value()
        previous_ids = getattr(self, "_timeline_ids", None)
        at_bottom = previous_ids is None or bar.maximum() - saved_scroll < 24
        # Opening (or re-clicking) a conversation always starts at the newest
        # message; otherwise only follow when the reader is already at the end.
        follow = bool(at_bottom or session_changed
                      or getattr(self, "_follow_tail", False))
        anchor = None if follow else self._scroll_anchor()
        self._follow_tail = follow
        self._timeline_rebuilding = True
        self._timeline_ids = {b.get("id") for b in messages}
        self._timeline_session = (kind, key)
        fresh = 0 if previous_ids is None else sum(
            1 for b in messages if b.get("id") not in previous_ids)
        self._timeline_signature = signature
        self._render_seq = getattr(self, "_render_seq", 0) + 1
        sequence = self._render_seq
        self._user_scrolled = False
        try:
            self._apply_timeline(items)
        except Exception:
            self._timeline_rebuilding = False
            raise
        QTimer.singleShot(0, lambda: self._settle_timeline_scroll(
            follow, saved_scroll, fresh, anchor, sequence))

    # ---------- incremental transcript ----------
    #
    # The old renderer tore the whole transcript down on every change: one
    # status flip re-decoded every image and lost the reader's place. Rows are
    # now mounted once and keyed by message id; each poll diffs the desired key
    # list against the mounted one and touches only what actually changed.

    def _timeline_items(self, messages: list[dict], query: str) -> list[tuple]:
        """Desired rows in display order: (key, kind, payload, signature)."""
        if not messages:
            text = ("没有匹配的消息" if query
                    else "还没有消息。发送一条文本或文件开始会话。")
            return [("empty", "empty", text, ("empty", text))]
        items = []
        last_day = None
        for bubble in messages:
            ts = datetime.fromtimestamp((bubble.get("time") or 0) / 1000)
            if ts.date() != last_day:
                last_day = ts.date()
                items.append(("day:" + last_day.isoformat(), "day", last_day,
                              ("day", last_day)))
            content = bubble.get("content", "")
            signature = (bubble.get("id"), bubble.get("status"),
                         bubble.get("time"), self._search_text(bubble),
                         repr(content) if isinstance(content, dict) else content)
            items.append(("msg:" + str(bubble.get("id")), "msg", bubble,
                          signature))
        return items

    def _is_safe_append(self, items: list[tuple]) -> bool:
        old_keys = getattr(self, "_timeline_keys", [])
        if len(items) < len(old_keys):
            return False
        if [item[0] for item in items[:len(old_keys)]] != old_keys:
            return False
        return all(self._timeline_row_sig.get(item[0]) == item[3]
                   for item in items[:len(old_keys)])

    def _clear_timeline(self) -> None:
        for key in list(getattr(self, "_timeline_keys", [])):
            self._unmount_row(key)
        self._timeline_keys = []
        self._timeline_ids = set()
        self._timeline_session = None

    def _unmount_row(self, key: str) -> None:
        widget = self._timeline_rows.pop(key, None)
        self._timeline_row_sig.pop(key, None)
        if widget is not None:
            self.timeline_layout.removeWidget(widget)
            widget.setParent(None)
            widget.deleteLater()

    def _mount_row(self, item: tuple, index: int) -> None:
        key, kind, payload, signature = item
        if kind == "empty":
            widget = QLabel(payload)
            widget.setObjectName("muted")
            widget.setAlignment(Qt.AlignCenter)
        elif kind == "day":
            widget = QLabel(payload.strftime("%m 月 %d 日"))
            widget.setObjectName("daySeparator")
            widget.setAlignment(Qt.AlignCenter)
        else:
            bubble = payload
            ts = datetime.fromtimestamp((bubble.get("time") or 0) / 1000)
            raw = bubble.get("content", "")
            outgoing = bubble.get("from") == self.self_id
            widget = QWidget()
            line = QHBoxLayout(widget)
            line.setContentsMargins(4, 0, 4, 0)
            image = self._image_path(raw) if isinstance(raw, dict) else ""
            if image:
                inner = self._image_bubble(image, outgoing, ts)
            elif isinstance(raw, dict):
                inner = self._file_bubble(raw, outgoing, ts)
            else:
                inner = self._bubble(str(raw), outgoing, ts)
            if outgoing:
                line.addStretch()
            line.addWidget(inner)
            if not outgoing:
                line.addStretch()
        self.timeline_layout.insertWidget(index, widget)
        self._timeline_rows[key] = widget
        self._timeline_row_sig[key] = signature

    def _apply_timeline(self, items: list[tuple]) -> None:
        old_keys = list(getattr(self, "_timeline_keys", []))
        new_keys = [item[0] for item in items]
        new_by_key = {item[0]: item for item in items}
        new_set = set(new_keys)
        # History only ever grows at the tail, so survivors normally form a
        # prefix of the desired order (the 100-message cap drops rows at the
        # front). Anything else (search rewrite, out-of-band edit) is rare and
        # falls back to a clean rebuild.
        survivors = [key for key in old_keys if key in new_set]
        if survivors != new_keys[:len(survivors)]:
            for key in old_keys:
                self._unmount_row(key)
        else:
            for key in old_keys:
                if key not in new_set:
                    self._unmount_row(key)
        for index, key in enumerate(new_keys):
            if key not in self._timeline_rows:
                self._mount_row(new_by_key[key], index)
        # Status/progress changes keep their key; redraw just that row.
        for index, key in enumerate(new_keys):
            if key not in self._timeline_rows:
                continue
            if self._timeline_row_sig.get(key) == new_by_key[key][3]:
                continue
            self._unmount_row(key)
            self._mount_row(new_by_key[key], index)
        self._timeline_keys = new_keys

    def _scroll_anchor(self):
        """(row key, offset from viewport top) for the first visible row."""
        bar = self.timeline.verticalScrollBar()
        top = bar.value()
        for key in getattr(self, "_timeline_keys", []):
            widget = self._timeline_rows.get(key)
            if widget is None:
                continue
            if widget.y() + widget.height() > top:
                return key, widget.y() - top
        return None

    def _restore_anchor(self, anchor) -> bool:
        if not anchor:
            return False
        key, offset = anchor
        widget = self._timeline_rows.get(key)
        if widget is None:
            return False
        bar = self.timeline.verticalScrollBar()
        target = max(0, widget.y() - offset)
        self._anchor_target = target
        bar.setValue(target)
        return True

    def _reassert_anchor(self, anchor) -> None:
        if self._timeline_rebuilding or self._follow_tail or self._user_scrolled:
            return
        self._restore_anchor(anchor)

    def _settle_timeline_scroll(self, follow: bool, saved_scroll: int,
                                fresh: int, anchor=None,
                                sequence: int | None = None) -> None:
        """Follow the newest message only when the reader is already at the end."""
        if sequence is not None and sequence != getattr(self, "_render_seq", sequence):
            return  # a newer frame already took over
        bar = self.timeline.verticalScrollBar()
        pill = getattr(self, "new_msg_btn", None)
        self._timeline_rebuilding = False
        self._follow_tail = follow
        if follow:
            self._scroll_to_bottom()
            if pill is not None:
                pill.hide()
            return
        if not self._restore_anchor(anchor):
            self._anchor_target = None
            bar.setValue(min(saved_scroll, max(0, bar.maximum())))
        QTimer.singleShot(0, lambda: self._reassert_anchor(anchor))
        QTimer.singleShot(80, lambda: self._reassert_anchor(anchor))
        if fresh and pill is not None:
            pill.setText(f"↓ {fresh} 条新消息")
            pill.show()

    def _scroll_to_bottom(self) -> None:
        """Pin to the tail; the range is only final once the layout settles."""
        self._anchor_target = None
        bar = self.timeline.verticalScrollBar()
        bar.setValue(bar.maximum())
        QTimer.singleShot(0, self._reassert_bottom)
        QTimer.singleShot(80, self._reassert_bottom)

    def _reassert_bottom(self) -> None:
        if self._timeline_rebuilding or not self._follow_tail:
            return
        bar = self.timeline.verticalScrollBar()
        bar.setValue(bar.maximum())

    def jump_to_latest(self) -> None:
        self._follow_tail = True
        self._scroll_to_bottom()
        self.new_msg_btn.hide()

    def _on_timeline_scroll(self, value: int) -> None:
        """Remember whether the reader is at the tail, and hide the pill there."""
        if self._timeline_rebuilding:
            return
        bar = self.timeline.verticalScrollBar()
        at_bottom = bar.maximum() - value < 24
        self._follow_tail = at_bottom
        pill = getattr(self, "new_msg_btn", None)
        if pill is not None and pill.isVisible() and at_bottom:
            pill.hide()

    def _on_timeline_range_changed(self, _minimum: int, _maximum: int) -> None:
        """Keep the tail in view while following; never fight a reader above it."""
        if self._timeline_rebuilding or not self._follow_tail:
            return
        bar = self.timeline.verticalScrollBar()
        bar.setValue(bar.maximum())

    IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic"}

    # ---------- copying ----------

    def timeline_selection(self) -> str:
        """Whatever the user has highlighted across the bubbles of the chat."""
        selected = []
        for label in self.timeline_widget.findChildren(QLabel):
            if label.textInteractionFlags() & (
                    Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard):
                text = (label.selectedText()
                        .replace(chr(0x2028), chr(10))
                        .replace(chr(0x2029), chr(10)).strip())
                if text:
                    selected.append(text)
        return "\n".join(selected)

    def copy_selection(self):
        text = self.timeline_selection()
        if text:
            QApplication.clipboard().setText(text)
            self.statusBar().showMessage(f"已复制 {len(text)} 个字符", 2500)

    def copy_all_messages(self):
        """Everything in the open conversation, oldest first, sender prefixed."""
        if not self.current:
            return
        kind, key = self.current["kind"], self.current["key"]
        lines = []
        for bubble in reversed(self.payload.get("bubbles") or []):
            if kind == "device" and key not in (bubble.get("from"), bubble.get("to")):
                continue
            if kind == "group" and bubble.get("groupId") != key:
                continue
            stamp = datetime.fromtimestamp((bubble.get("time") or 0) / 1000)
            who = "我" if bubble.get("from") == self.self_id else self._peer_label()
            content = bubble.get("content")
            body = ("[文件] " + str(content.get("name", ""))) if isinstance(content, dict) \
                else str(content or "")
            lines.append(f"{stamp.strftime('%m-%d %H:%M')} {who}：{body}")
        if not lines:
            self.statusBar().showMessage("这个会话还没有消息", 2500)
            return
        QApplication.clipboard().setText("\n".join(lines))
        self.statusBar().showMessage(f"已复制 {len(lines)} 条消息", 3000)

    def _peer_label(self) -> str:
        key = self.current["key"] if self.current else ""
        for device in self.payload.get("devices") or []:
            if device.get("fingerprint") == key:
                return device.get("alias") or key[:8]
        return key[:8]

    def _canvas_menu(self, pos):
        menu = QMenu(self.timeline_widget)
        if self.timeline_selection():
            menu.addAction("复制所选", self.copy_selection)
        menu.addAction("全选会话文本", self.select_all_bubbles)
        menu.addAction("复制整个会话", self.copy_all_messages)
        menu.exec(self.timeline_widget.mapToGlobal(pos))

    def select_all_bubbles(self):
        for label in self.timeline_widget.findChildren(QLabel):
            if label.textInteractionFlags() & (
                    Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard):
                label.selectAll()

    def _bubble_menu(self, label: QLabel, menu_anchor: QWidget) -> None:
        menu = QMenu(menu_anchor)
        selected = self.timeline_selection()
        if selected:
            menu.addAction("复制所选", self.copy_selection)
        menu.addAction("复制这条消息",
                       lambda: QApplication.clipboard().setText(label.text()))
        menu.addAction("复制整个会话", self.copy_all_messages)
        menu.exec(menu_anchor.mapToGlobal(menu_anchor.rect().bottomLeft()))

    @staticmethod
    def _search_text(bubble: dict) -> str:
        content = bubble.get("content", "")
        if isinstance(content, dict):
            return str(content.get("name", ""))
        return str(content)

    def _image_path(self, content: dict) -> str:
        """A received image keeps its local path in the bubble payload."""
        for key in ("path", "savePath", "filePath", "local", "downloadPath"):
            value = content.get(key)
            if isinstance(value, str) and value:
                file = Path(value)
                if file.suffix.lower() in self.IMAGE_SUFFIXES and file.is_file():
                    return str(file)
        return ""

    def _pixmap_for(self, path: str) -> QPixmap:
        """Decode once per (path, mtime, size); keep the 260 px version only."""
        try:
            stat = os.stat(path)
            cache_key = (path, stat.st_mtime_ns, stat.st_size)
        except OSError:
            cache_key = (path, 0, 0)
        cached = self._pixmap_cache.get(cache_key)
        if cached is not None:
            return cached
        pixmap = QPixmap(path)
        if not pixmap.isNull():
            pixmap = pixmap.scaled(260, 260, Qt.KeepAspectRatio,
                                   Qt.SmoothTransformation)
            self._pixmap_cache[cache_key] = pixmap
            while len(self._pixmap_cache) > 32:
                self._pixmap_cache.pop(next(iter(self._pixmap_cache)))
        return pixmap

    def _image_bubble(self, path: str, outgoing: bool, ts: datetime) -> QFrame:
        frame = QFrame()
        frame.setObjectName("bubbleOut" if outgoing else "bubbleIn")
        box = QVBoxLayout(frame)
        box.setContentsMargins(6, 6, 6, 6)
        picture = QLabel()
        pixmap = self._pixmap_for(path)
        if pixmap.isNull():
            picture.setText("图片无法解码")
            picture.setObjectName("bubbleText")
        else:
            picture.setPixmap(pixmap)
        picture.setToolTip(path)
        picture.setCursor(Qt.PointingHandCursor)
        picture.mouseDoubleClickEvent = lambda event, target=path: self.open_path(target)
        box.addWidget(picture)
        stamp = QLabel(ts.strftime("%H:%M"))
        stamp.setObjectName("bubbleMeta")
        stamp.setAlignment(Qt.AlignRight)
        box.addWidget(stamp)
        frame.setMaximumWidth(300)
        return frame

    def _file_bubble(self, content: dict, outgoing: bool, ts: datetime) -> QFrame:
        """Non-image attachment: name, size, transfer state, double-click to open."""
        frame = QFrame()
        frame.setObjectName("bubbleOut" if outgoing else "bubbleIn")
        box = QVBoxLayout(frame)
        box.setContentsMargins(12, 10, 12, 8)
        box.setSpacing(2)
        head = QHBoxLayout()
        head.setSpacing(7)
        glyph = QLabel()
        glyph.setPixmap(icon("file", "#8a978f", 16).pixmap(16, 16))
        glyph.setAlignment(Qt.AlignTop)
        head.addWidget(glyph, 0)
        name = QLabel(str(content.get("name") or "文件"))
        name.setObjectName("bubbleText")
        name.setWordWrap(True)
        head.addWidget(name, 1)
        box.addLayout(head)
        state = content.get("state")
        detail = self.format_size(content.get("size", 0))
        if state is not None and int(state) != 2:
            detail += f" · {float(content.get('progress', 0)) * 100:.0f}%"
        meta = QLabel(detail)
        meta.setObjectName("bubbleMeta")
        box.addWidget(meta)
        path = content.get("path") or ""
        if path and Path(path).exists():
            frame.setToolTip(path + "\n双击打开")
            frame.setCursor(Qt.PointingHandCursor)
            frame.mouseDoubleClickEvent = lambda event, target=path: self.open_path(target)
        stamp = QLabel(ts.strftime("%H:%M"))
        stamp.setObjectName("bubbleMeta")
        stamp.setAlignment(Qt.AlignRight)
        box.addWidget(stamp)
        frame.setMaximumWidth(320)
        return frame

    def open_path(self, path: str):
        try:
            self._open_local(path)
        except OSError as exc:
            self.statusBar().showMessage(f"打不开：{exc}", 4000)

    def _bubble(self, content: str, outgoing: bool, ts: datetime) -> QFrame:
        frame = QFrame()
        frame.setObjectName("bubbleOut" if outgoing else "bubbleIn")
        box = QVBoxLayout(frame)
        box.setContentsMargins(14, 9, 14, 7)
        text = QLabel(content)
        text.setWordWrap(True)
        text.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        text.setFocusPolicy(Qt.ClickFocus)
        text.setContextMenuPolicy(Qt.CustomContextMenu)
        text.customContextMenuRequested.connect(
            lambda _pos, label=text: self._bubble_menu(label, label))
        text.setObjectName("bubbleText")
        box.addWidget(text)
        stamp = QLabel(ts.strftime("%H:%M"))
        stamp.setObjectName("bubbleMeta")
        stamp.setAlignment(Qt.AlignRight)
        box.addWidget(stamp)
        frame.setMaximumWidth(560)
        return frame

    # ---------- actions ----------

    def send_message(self):
        text = self.composer.toPlainText().strip()
        if not text or not self.current or self.current["kind"] not in ("device", "group"):
            return
        path = "/api/send-text" if self.current["kind"] == "device" else "/api/groups/send"
        key = "to" if self.current["kind"] == "device" else "group"
        self.run_action(lambda: self.api.post(path, {key: self.current["key"], "text": text}),
                        lambda _result: self.composer.clear())

    def choose_files(self, images_only=False):
        if not self.current or self.current["kind"] != "device":
            self.statusBar().showMessage("请选择一台设备后发送附件")
            return
        filt = "图片 (*.png *.jpg *.jpeg *.gif *.webp);;所有文件 (*)" if images_only else "所有文件 (*)"
        paths, _ = QFileDialog.getOpenFileNames(self, "选择要发送的文件", "", filt)
        if paths:
            self.run_action(lambda: self.api.post("/api/send-files",
                                                  {"to": self.current["key"], "paths": paths}))

    def send_clipboard(self):
        if not self.current or self.current["kind"] != "device":
            return
        text = QApplication.clipboard().text()
        if not text:
            self.statusBar().showMessage("剪贴板中没有文本")
            return
        self.run_action(lambda: self.api.post("/api/send-clipboard",
                                              {"to": self.current["key"], "text": text}))

    def show_more(self):
        menu = QMenu(self)
        kind = (self.current or {}).get("kind")
        if kind == "group":
            menu.addAction("群设置…", self.group_settings_dialog)
            menu.addSeparator()
        elif kind == "device":
            key = (self.current or {}).get("key")
            paired = key in set(self.payload.get("paired") or [])
            action = menu.addAction("已配对 · 无需再配" if paired else "与这台设备配对",
                                    self.pair_selected_device)
            action.setEnabled(not paired)
            menu.addSeparator()
        menu.addAction("刷新会话", self.refresh)
        menu.addAction("查看传输任务", lambda: self.set_mode("transfers"))
        menu.addAction("设置与状态", lambda: self.set_mode("settings"))
        menu.exec(self.cursor().pos())

    def pair_selected_device(self):
        key = (self.current or {}).get("key")
        device = next((d for d in self.payload.get("devices") or []
                       if d.get("fingerprint") == key), None)
        if device is None:
            self.statusBar().showMessage("先选一台设备", 3000)
            return
        label = device.get("alias") or key[:8]
        self.statusBar().showMessage(f"正在向 {label} 发起配对，请在对方设备上确认…", 8000)
        self.run_action(
            lambda: self.api.post("/api/pair", {"to": key}),
            lambda _result: self.statusBar().showMessage(f"已与 {label} 配对", 6000))

    # ---------- group management ----------

    def _member_label(self, fingerprint: str) -> str:
        for device in self.payload.get("devices") or []:
            if device.get("fingerprint") == fingerprint:
                return f"{device.get('alias') or fingerprint[:8]} · {device.get('ip') or '离线'}"
        return fingerprint[:8] + "（未知设备）"

    def group_settings_dialog(self):
        group_id = (self.current or {}).get("key")
        group = next((g for g in self.payload.get("groups") or []
                      if g.get("id") == group_id), None)
        if group is None:
            self.statusBar().showMessage("先选一个群组", 3000)
            return
        dialog = QDialog(self)
        dialog.setObjectName("groupDialog")
        dialog.setWindowTitle("群设置")
        dialog.setFixedWidth(460)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        caption = QLabel("群名称")
        caption.setObjectName("muted")
        layout.addWidget(caption)
        name_edit = QLineEdit(group.get("name") or "")
        name_edit.setPlaceholderText("群聊名称")
        layout.addWidget(name_edit)

        caption = QLabel("成员")
        caption.setObjectName("muted")
        layout.addWidget(caption)
        member_list = QListWidget()
        member_list.setObjectName("memberList")
        member_list.setMinimumHeight(140)
        layout.addWidget(member_list, 1)

        removed: set[str] = set()
        added: set[str] = set()

        def refresh_members():
            member_list.clear()
            for fingerprint in list(group.get("members") or []) + list(added):
                if fingerprint in removed or fingerprint in added:
                    continue
                item = QListWidgetItem(self._member_label(fingerprint))
                item.setData(Qt.UserRole, fingerprint)
                member_list.addItem(item)
            for fingerprint in added:
                item = QListWidgetItem(self._member_label(fingerprint) + " · 待添加")
                item.setData(Qt.UserRole, fingerprint)
                member_list.addItem(item)

        refresh_members()

        row = QHBoxLayout()
        add_button = QPushButton("添加成员…")
        remove_button = QPushButton("移出所选")

        def add_member():
            mine = set(group.get("members") or []) | added
            candidates = [(d.get("fingerprint"), d.get("alias") or d.get("fingerprint", "")[:8])
                          for d in self.payload.get("devices") or []
                          if d.get("fingerprint") and d["fingerprint"] not in mine
                          and d.get("fingerprint") in set(self.payload.get("paired") or [])]
            if not candidates:
                self.statusBar().showMessage("没有可添加的已配对设备", 4000)
                return
            from PySide6.QtWidgets import QInputDialog
            labels = [f"{alias}" for _fp, alias in candidates]
            choice, ok = QInputDialog.getItem(self, "添加成员", "选择已配对设备", labels, 0, False)
            if ok and choice:
                fingerprint = candidates[labels.index(choice)][0]
                added.add(fingerprint)
                refresh_members()

        def remove_selected():
            item = member_list.currentItem()
            if item is None:
                return
            fingerprint = item.data(Qt.UserRole)
            added.discard(fingerprint)
            removed.add(fingerprint)
            refresh_members()

        add_button.clicked.connect(add_member)
        remove_button.clicked.connect(remove_selected)
        row.addWidget(add_button)
        row.addWidget(remove_button)
        row.addStretch()
        layout.addLayout(row)

        actions = QHBoxLayout()
        hint = QLabel("改动会推送给所有在线成员")
        hint.setObjectName("muted")
        actions.addWidget(hint)
        actions.addStretch()
        cancel = QPushButton("取消")
        save = QPushButton("保存")
        save.setObjectName("primary")
        cancel.clicked.connect(dialog.reject)
        save.clicked.connect(dialog.accept)
        actions.addWidget(cancel)
        actions.addWidget(save)
        layout.addLayout(actions)

        if dialog.exec() != QDialog.Accepted:
            return
        payload = {
            "group": group_id,
            "name": name_edit.text().strip(),
            "add": sorted(added),
            "remove": sorted(removed),
        }
        if not payload["name"] and not payload["add"] and not payload["remove"]:
            return
        self.run_action(lambda: self.api.post("/api/groups/update", payload))

    def show_add_menu(self):
        menu = QMenu(self)
        menu.addAction("新建群组…", self.create_group_dialog)
        menu.addAction("手动添加设备…", self.manual_add_dialog)
        menu.exec(self.cursor().pos())

    def create_group_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("新建群组")
        dialog.setFixedWidth(460)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)
        caption = QLabel("群名称")
        caption.setObjectName("muted")
        layout.addWidget(caption)
        name_edit = QLineEdit()
        name_edit.setPlaceholderText("例如：研发群")
        layout.addWidget(name_edit)
        caption = QLabel("勾选成员（只列已配对设备）")
        caption.setObjectName("muted")
        layout.addWidget(caption)
        member_list = QListWidget()
        member_list.setObjectName("memberList")
        member_list.setMinimumHeight(150)
        paired = set(self.payload.get("paired") or [])
        for device in self.payload.get("devices") or []:
            fingerprint = device.get("fingerprint")
            if fingerprint not in paired:
                continue
            item = QListWidgetItem(f"{device.get('alias') or fingerprint[:8]} · "
                                   f"{device.get('ip') or '离线'}")
            item.setData(Qt.UserRole, fingerprint)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            member_list.addItem(item)
        if member_list.count() == 0:
            empty = QLabel("还没有已配对设备。先到设备页完成配对，群才能在两端同步。")
            empty.setObjectName("muted")
            empty.setWordWrap(True)
            layout.addWidget(empty)
        layout.addWidget(member_list, 1)
        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton("取消")
        create = QPushButton("创建")
        create.setObjectName("primary")
        cancel.clicked.connect(dialog.reject)
        create.clicked.connect(dialog.accept)
        actions.addWidget(cancel)
        actions.addWidget(create)
        layout.addLayout(actions)

        if dialog.exec() != QDialog.Accepted:
            return
        name = name_edit.text().strip()
        if not name:
            self.statusBar().showMessage("群名称不能为空", 4000)
            return
        members = [member_list.item(i).data(Qt.UserRole)
                   for i in range(member_list.count())
                   if member_list.item(i).checkState() == Qt.Checked]
        self.run_action(
            lambda: self.api.post("/api/groups/create", {"name": name, "members": members}),
            lambda _result: self.set_mode("groups"))

    def manual_add_dialog(self):
        from PySide6.QtWidgets import QDialog, QDialogButtonBox
        dialog = QDialog(self)
        dialog.setWindowTitle("手动添加设备")
        dialog.setFixedWidth(400)
        layout = QVBoxLayout(dialog)
        note = QLabel("探测对端是否在线。配对请对可达设备执行 flix pair <ip:port>。")
        note.setObjectName("muted")
        note.setWordWrap(True)
        input_edit = QLineEdit()
        input_edit.setPlaceholderText("192.168.1.23:8891")
        layout.addWidget(note)
        layout.addWidget(input_edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("探测")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        raw = input_edit.text().strip()
        if not raw:
            return
        head, _, tail = raw.rpartition(":")
        ip = head or raw
        port = int(tail) if tail.isdigit() else 8891
        self.run_action(lambda: self._probe_peer(ip, port))

    def _probe_peer(self, ip: str, port: int) -> dict:
        import asyncio
        from .channel import ping_v2
        from .config import Store, load_or_create_identity

        async def run():
            store = Store()
            try:
                ident = load_or_create_identity(store)
                return await ping_v2(ip, ident, port=port)
            finally:
                store.close()

        modal = asyncio.run(run())
        return {"ok": True, "alias": modal.get("alias", ""),
                "fingerprint": modal.get("fingerprint", ""),
                "hint": "设备可达；执行 flix pair 完成配对"}

    def populate_transfers(self):
        self.transfer_list.clear()
        files = [b for b in self.payload.get("bubbles") or [] if isinstance(b.get("content"), dict)]
        for bubble in files:
            data = bubble["content"]
            item = QListWidgetItem(
                f"▤  {data.get('name', '文件')}\n    {self.format_size(data.get('size', 0))} · "
                f"{float(data.get('progress', 0)) * 100:.0f}%")
            self.transfer_list.addItem(item)
        if not files:
            self.transfer_list.addItem("暂无文件传输")

    def populate_adb(self):
        self.adb_list.clear()
        devices = (self.payload.get("adb") or {}).get("devices", [])
        for device in devices:
            self.adb_list.addItem(
                f"{'●' if device.get('authorized') else '○'}  {device.get('model') or 'Android'}\n"
                f"    {device.get('serial')} · {device.get('state')}")
        if not devices:
            self.adb_list.addItem("未检测到 ADB 设备")

    def populate_remote(self):
        selected = self.remote_sessions.currentItem().data(Qt.UserRole) if self.remote_sessions.currentItem() else None
        self.remote_sessions.clear()
        for session in (self.payload.get("media") or {}).get("sessions", []):
            peer = session.get("receiver") if session.get("initiator") == self.self_id else session.get("initiator")
            item = QListWidgetItem(
                f"{session.get('state')} · {str(peer)[:12]} · "
                f"{', '.join(session.get('approved') or session.get('requested', []))}")
            item.setData(Qt.UserRole, session.get("callId"))
            self.remote_sessions.addItem(item)
            if item.data(Qt.UserRole) == selected:
                self.remote_sessions.setCurrentItem(item)
        self.update_remote_actions()

    def selected_remote(self):
        item = self.remote_sessions.currentItem()
        call_id = item.data(Qt.UserRole) if item else None
        return next((s for s in (self.payload.get("media") or {}).get("sessions", [])
                     if s.get("callId") == call_id), None)

    def request_remote(self):
        target = self.current["key"] if self.current and self.current["kind"] == "device" else None
        if not target:
            self.statusBar().showMessage("请选择一台设备")
            return
        caps = ["view"] + (["control"] if self.remote_control.isChecked() else [])
        self.workspace_stack.setCurrentIndex(5)
        self.run_action(lambda: self.api.post("/api/media/request",
                                              {"deviceId": target, "capabilities": caps}))

    def decide_remote(self, caps):
        session = self.selected_remote()
        if session:
            caps = [cap for cap in caps if cap in session.get("requested", [])]
            self.run_action(lambda: self.api.post("/api/media/decision",
                                                  {"callId": session["callId"],
                                                   "approve": True, "capabilities": caps}))

    def deny_selected_remote(self):
        session = self.selected_remote()
        if session:
            self.run_action(lambda: self.api.post("/api/media/decision",
                                                  {"callId": session["callId"], "approve": False}))

    def connect_selected_remote(self):
        session = self.selected_remote()
        if session:
            self.run_action(lambda: self.api.post("/api/media/offer", {"callId": session["callId"]}))

    def revoke_selected_remote(self):
        session = self.selected_remote()
        if session:
            self.run_action(lambda: self.api.post("/api/media/revoke", {"callId": session["callId"]}))

    def update_remote_actions(self):
        session = self.selected_remote()
        pending = bool(session and session.get("state") == "pending" and session.get("receiver") == self.self_id)
        approved = bool(session and session.get("state") == "approved" and session.get("initiator") == self.self_id)
        active = bool(session and session.get("state") == "active")
        self.approve_view.setEnabled(pending)
        self.approve_control.setEnabled(pending and "control" in session.get("requested", []))
        self.deny_remote.setEnabled(pending)
        self.connect_remote.setEnabled(approved)
        self.revoke_remote.setEnabled(bool(session and session.get("state") not in ("closed", "denied", "expired")))
        if session:
            self.remote_status.setText(
                f"{session.get('state')} · 请求 {', '.join(session.get('requested', []))} · "
                f"批准 {', '.join(session.get('approved', [])) or '无'}")
            if active:
                self.load_snapshot(session["callId"])

    def load_snapshot(self, call_id):
        try:
            pixmap = QPixmap()
            if pixmap.loadFromData(self.api.bytes(f"/api/media/{call_id}/snapshot")):
                self.remote_preview.setPixmap(pixmap.scaled(
                    self.remote_preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        except ApiError:
            self.remote_preview.setText("正在等待远端画面…")

    def run_action(self, fn, on_success=None):
        if self.action_worker and self.action_worker.isRunning():
            self.statusBar().showMessage("上一项操作仍在进行")
            return
        self.action_worker = ApiThread(fn)
        self.action_worker.result.connect(lambda result: self.action_done(result, on_success))
        self.action_worker.failed.connect(self.fail)
        self.action_worker.start()

    def action_done(self, result, on_success):
        if isinstance(result, dict) and result.get("ok") is False:
            self.statusBar().showMessage(result.get("detail") or result.get("error", "操作失败"))
            return
        if on_success:
            on_success(result)
        self.statusBar().showMessage("操作已完成")
        self.refresh()

    def fail(self, detail):
        self.connection_dot.setProperty("online", False)
        self.connection_dot.style().unpolish(self.connection_dot)
        self.connection_dot.style().polish(self.connection_dot)
        # 8898 may be the OFFICIAL Flix Desktop API (it answers /api/self but
        # has no /api/overview). Before declaring offline, probe the candidate
        # ports for a real flixcli service.
        self.offline_mode = True
        self.statusBar().showMessage("无法连接 flixd：" + detail + "（正在探测候选端口…）")
        self._discovery_worker = ApiThread(lambda: LocalApi.discover(self.api_port))
        self._discovery_worker.result.connect(self._discovery_done)
        self._discovery_worker.start()

    def _discovery_done(self, found):
        if found is not None:
            self.api = found
            self.api_port = int(found.base.rsplit(":", 1)[-1])
            self.offline_mode = False
            self.statusBar().showMessage(f"已连接本机服务 :{self.api_port}")
            self.refresh()
            return
        if not getattr(self, "_auto_start_tried", False):
            # One software = double-click and it works. Spawn the background
            # service once by itself; the offline page stays as the fallback
            # when that fails, with the manual button still there.
            self._auto_start_tried = True
            self.start_local_daemon()
            return
        self.workspace_stack.setCurrentIndex(6)
        self.statusBar().showMessage(
            "未连接本机服务，可从下方一键启动（与原生 Flix 端口共存）")

    def eventFilter(self, watched, event):
        timeline = getattr(self, "timeline", None)
        if timeline is not None and watched in (
                timeline, timeline.viewport(), timeline.verticalScrollBar()):
            if event.type() in (QEvent.Wheel, QEvent.KeyPress) or (
                    event.type() in (QEvent.MouseButtonPress, QEvent.MouseMove)
                    and event.buttons()):
                self._user_scrolled = True
        if watched is self.composer and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                # 回车直接发送；Shift+回车换行。Ctrl+回车保留为发送的旧习惯。
                # 输入法组词时的回车由 IME 自己消费，不会走到这里。
                if not (event.modifiers() & Qt.ShiftModifier):
                    self.send_message(); return True
        if watched is getattr(self, "remote_preview", None):
            session = self.selected_remote()
            allowed = bool(session and session.get("state") == "active" and
                           "control" in session.get("approved", []))
            if not allowed:
                return False
            payload = self.control_event(event)
            if payload:
                self.run_action(lambda: self.api.post("/api/media/control",
                                                       {"callId": session["callId"], "event": payload}))
                return True
        return super().eventFilter(watched, event)

    def control_event(self, event):
        self.media_seq += 1
        base = {"v": 1, "seq": self.media_seq}
        if event.type() == QEvent.MouseMove:
            pos = event.position()
            return {**base, "kind": "pointer",
                    "x": max(0.0, min(1.0, pos.x() / max(1, self.remote_preview.width()))),
                    "y": max(0.0, min(1.0, pos.y() / max(1, self.remote_preview.height())))}
        if event.type() in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
            names = {Qt.LeftButton: "left", Qt.RightButton: "right", Qt.MiddleButton: "middle"}
            if event.button() in names:
                return {**base, "kind": "button", "button": names[event.button()],
                        "action": "down" if event.type() == QEvent.MouseButtonPress else "up"}
        if event.type() == QEvent.Wheel:
            return {**base, "kind": "wheel", "delta": max(-1200, min(1200, event.angleDelta().y()))}
        if event.type() in (QEvent.KeyPress, QEvent.KeyRelease):
            if event.modifiers() != Qt.NoModifier or event.isAutoRepeat():
                return None
            names = {Qt.Key_Backspace: "backspace", Qt.Key_Tab: "tab", Qt.Key_Return: "enter",
                     Qt.Key_Enter: "enter", Qt.Key_Escape: "escape", Qt.Key_Space: "space",
                     Qt.Key_PageUp: "pageup", Qt.Key_PageDown: "pagedown", Qt.Key_End: "end",
                     Qt.Key_Home: "home", Qt.Key_Left: "left", Qt.Key_Up: "up",
                     Qt.Key_Right: "right", Qt.Key_Down: "down", Qt.Key_Insert: "insert",
                     Qt.Key_Delete: "delete"}
            key = names.get(event.key())
            if key is None and Qt.Key_A <= event.key() <= Qt.Key_Z:
                key = chr(event.key()).lower()
            if key is None and Qt.Key_0 <= event.key() <= Qt.Key_9:
                key = chr(event.key())
            if key is None and Qt.Key_F1 <= event.key() <= Qt.Key_F12:
                key = f"f{event.key() - Qt.Key_F1 + 1}"
            if key:
                return {**base, "kind": "key", "key": key,
                        "action": "down" if event.type() == QEvent.KeyPress else "up"}
        return None

    @staticmethod
    def format_size(value):
        size = float(value or 0)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024


def toast_hint(parent, text: str):
    parent.statusBar().showMessage(text)


def main(argv: list[str] | None = None, *, api_port: int = 8898) -> int:
    app = QApplication(argv or sys.argv)
    app.setApplicationName("RelayDesk")
    app.setWindowIcon(logo_icon())
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 9))
    window = MainWindow(api_port)
    window.setWindowIcon(logo_icon())
    window.show()
    return app.exec()


PREFS_FILE = Path.home() / ".relaydesk_gui.json"


PALETTES = {
    "dark": {
        "bg": "#0e1211", "bg_rail": "#121715", "bg_side": "#141a18",
        "surface": "#1a211e", "surface_hi": "#202824",
        "border": "#2a332f", "border_hi": "#3a463f",
        "text": "#e9eeeb", "text_dim": "#b6c2bc", "muted": "#8a978f",
        "accent": "#3fa27b", "accent_hi": "#4ab68b", "accent_press": "#348a69",
        "accent_soft": "rgba(63,162,123,0.14)",
        "on_accent": "#06110c", "on_accent_meta": "rgba(6,17,12,0.62)",
        "danger": "#d96157", "danger_hi": "#e57369",
        "online": "#4fc38f", "offline": "#5c6b64",
        "menu_bg": "#161d1a", "header_bg": "rgba(18,23,21,0.92)",
        "scroll": "#33413a", "scroll_hi": "#46564d",
        "icon": "#8fa39a",
    },
    "light": {
        "bg": "#f1f0ea", "bg_rail": "#e9e7df", "bg_side": "#f7f6f1",
        "surface": "#ffffff", "surface_hi": "#f0efe8",
        "border": "#d9d5ca", "border_hi": "#c3bfb2",
        "text": "#1b221e", "text_dim": "#43514a", "muted": "#5f6c66",
        "accent": "#1f7a5c", "accent_hi": "#186a4f", "accent_press": "#145a43",
        "accent_soft": "rgba(31,122,92,0.10)",
        "on_accent": "#ffffff", "on_accent_meta": "rgba(255,255,255,0.78)",
        "danger": "#b4483f", "danger_hi": "#9e3c34",
        "online": "#1f7a5c", "offline": "#8a948e",
        "menu_bg": "#ffffff", "header_bg": "rgba(247,246,241,0.92)",
        "scroll": "#cfcabf", "scroll_hi": "#b8b3a6",
        "icon": "#5f6c66",
    },
}


def palette_for(theme: str) -> QPalette:
    """Qt palette for the parts QSS does not reach (menus, dialogs, tooltips)."""
    t = PALETTES["light" if theme == "light" else "dark"]
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(t["bg"]))
    pal.setColor(QPalette.WindowText, QColor(t["text"]))
    pal.setColor(QPalette.Base, QColor(t["surface"]))
    pal.setColor(QPalette.AlternateBase, QColor(t["bg_side"]))
    pal.setColor(QPalette.Text, QColor(t["text"]))
    pal.setColor(QPalette.Button, QColor(t["surface"]))
    pal.setColor(QPalette.ButtonText, QColor(t["text"]))
    pal.setColor(QPalette.ToolTipBase, QColor(t["surface"]))
    pal.setColor(QPalette.ToolTipText, QColor(t["text"]))
    pal.setColor(QPalette.PlaceholderText, QColor(t["muted"]))
    pal.setColor(QPalette.Highlight, QColor(t["accent"]))
    pal.setColor(QPalette.HighlightedText, QColor(t["on_accent"]))
    return pal


def styles_for(theme: str) -> str:
    """One authored sheet for both themes, filled from explicit tokens."""
    tokens = dict(PALETTES["light" if theme == "light" else "dark"])
    tokens["font_stack"] = FONT_STACK
    return Template(STYLES).substitute(tokens)


STYLES = """
* { font-family: $font_stack; }
QWidget#rootWindow, QWidget#page { background: $bg; color: $text; }
QPushButton { background: $surface; color: $text; border: 1px solid $border;
    border-radius: 8px; padding: 7px 12px; font-size: 12.5px; }
QPushButton:hover { background: $surface_hi; border-color: $border_hi; }
QPushButton:disabled { color: $muted; border-color: $border; }
QFrame#titleBar { background: $bg_rail; border-bottom: 1px solid $border; }
QLabel#titleName { color: $text_dim; font-size: 12px; font-weight: 600; }
QPushButton#winBtn, QPushButton#closeBtn { background: transparent; border: 0; border-radius: 0; }
QPushButton#winBtn:hover { background: $surface_hi; }
QPushButton#closeBtn:hover { background: $danger; }

QFrame#rail { background: $bg_rail; border-right: 1px solid $border; }
QPushButton#railButton { background: transparent; border: 0; border-radius: 10px; }
QPushButton#railButton:hover { background: $surface_hi; }
QPushButton#railButton:checked { background: $accent_soft; }
QLabel#connectionDot { color: $offline; font-size: 10px; }
QLabel#connectionDot[online="true"] { color: $online; }

QFrame#sidebar { background: $bg_side; border-right: 1px solid $border; }
QLabel#brand { color: $text; font-size: 17px; font-weight: 700; }
QLabel#brandSub { color: $muted; font-size: 11.5px; }
QPushButton#roundBtn { background: transparent; color: $text_dim;
    border: 1px solid $border; border-radius: 9px; }
QPushButton#roundBtn:hover { background: $surface_hi; border-color: $border_hi; }
QLineEdit#search, QLineEdit#sessionSearch { background: $surface; color: $text;
    border: 1px solid $border; border-radius: 9px; padding: 8px 11px; font-size: 12.5px;
    selection-background-color: $accent; selection-color: $on_accent; }
QLineEdit#search:focus, QLineEdit#sessionSearch:focus { border-color: $accent; }
QLabel#sidebarTitle { color: $muted; padding-top: 2px; font-weight: 600; font-size: 11px; }
QListWidget#entityList { background: transparent; border: 0; outline: 0; }
QListWidget#entityList::item { color: $text; margin: 2px 0; border-radius: 10px;
    border: 1px solid transparent; }
QListWidget#entityList::item:selected { background: $accent_soft; border: 1px solid $accent; }
QListWidget#entityList::item:hover { background: $surface_hi; }
QFrame#entityCard { background: transparent; }
QLabel#entityName { color: $text; font-size: 13px; font-weight: 600; }
QLabel#entitySub { color: $muted; font-size: 11.5px; }
QLabel#onlineDot { color: $online; font-size: 10px; }
QLabel#offlineDot { color: $offline; font-size: 10px; }

QFrame#workspace { background: $bg; }
QFrame#sessionHeader { background: $bg; border-bottom: 1px solid $border; }
QLabel#sessionTitle { color: $text; font-size: 16px; font-weight: 650; }
QLabel#muted { color: $muted; font-size: 12px; }
QPushButton#headerBtn { background: transparent; border: 1px solid $border; border-radius: 9px; }
QPushButton#headerBtn:hover { background: $surface_hi; border-color: $border_hi; }

QScrollArea#timeline { background: transparent; border: 0; }
QWidget#timelineCanvas { background: transparent; }
QLabel#daySeparator { color: $muted; font-size: 11px; padding: 6px 0 2px 0; }
QFrame#bubbleOut { background: $accent; border-radius: 12px; border-bottom-right-radius: 4px; }
QFrame#bubbleIn { background: $surface; border: 1px solid $border;
    border-radius: 12px; border-bottom-left-radius: 4px; }
QLabel#bubbleText { color: $text; font-size: 13px; }
QFrame#bubbleOut QLabel#bubbleText { color: $on_accent; }
QLabel#bubbleMeta { color: $muted; font-size: 10.5px; }
QFrame#bubbleOut QLabel#bubbleMeta { color: $on_accent_meta; }

QFrame#composer { background: $surface; border: 1px solid $border; border-radius: 12px; }
QTextEdit#composerEdit { background: transparent; border: 0; color: $text; font-size: 13px;
    selection-background-color: $accent; selection-color: $on_accent; }
QPushButton#chipBtn { background: transparent; border: 1px solid $border; border-radius: 8px; }
QPushButton#chipBtn:hover { background: $surface_hi; border-color: $border_hi; }
QPushButton#newMsgPill { background: $accent; color: $on_accent; border: 0;
    border-radius: 11px; padding: 5px 14px; font-size: 12px; font-weight: 600; }
QPushButton#newMsgPill:hover { background: $accent_hi; }
QPushButton#sendButton { background: $accent; color: $on_accent; border: 0; border-radius: 9px;
    padding: 8px 18px; font-weight: 600; font-size: 12.5px; }
QPushButton#sendButton:hover { background: $accent_hi; }
QPushButton#sendButton:pressed { background: $accent_press; }

QListWidget#contentList { background: $surface; color: $text; border: 1px solid $border;
    border-radius: 10px; padding: 6px; }
QListWidget#contentList::item { padding: 10px; border-bottom: 1px solid $border;
    border-radius: 6px; }
QListWidget#contentList::item:hover { background: $surface_hi; }
QLabel#pageTitle { color: $text; font-size: 20px; font-weight: 700; }
QLabel#emptyTitle { color: $text; font-size: 19px; font-weight: 650; }
QFrame#settingsCard { background: $surface; border: 1px solid $border; border-radius: 10px; }
QLabel#settingsText { color: $text_dim; font-size: 12.5px; }
QLabel#mediaPreview { background: $bg; color: $muted; border: 1px solid $border;
    border-radius: 10px; }
QCheckBox { color: $text_dim; spacing: 7px; font-size: 12.5px; }
QPushButton#primaryBtn { background: $accent; color: $on_accent; border: 0; border-radius: 9px;
    padding: 9px 18px; font-weight: 600; }
QPushButton#primaryBtn:hover { background: $accent_hi; }
QPushButton#secondaryButton { background: transparent; color: $text; border: 1px solid $border;
    border-radius: 9px; padding: 8px 14px; font-size: 12.5px; }
QPushButton#secondaryButton:hover { background: $surface_hi; border-color: $border_hi; }
QPushButton#secondaryButton:disabled { color: $muted; border-color: $border; }
QMenu { background: $menu_bg; color: $text; border: 1px solid $border; border-radius: 9px;
    padding: 5px; }
QMenu::item { padding: 8px 20px; border-radius: 6px; }
QMenu::item:selected { background: $accent_soft; color: $text; }
QStatusBar { background: $bg_rail; color: $muted; border-top: 1px solid $border;
    font-size: 11.5px; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: $scroll; border-radius: 5px; min-height: 36px; }
QScrollBar::handle:vertical:hover { background: $scroll_hi; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QToolTip { background: $menu_bg; color: $text; border: 1px solid $border; padding: 5px 8px; }
QPushButton:focus { border: 1px solid $accent; }
QPushButton#railButton:focus { border: 1px solid $accent; }
QPushButton#sendButton:focus, QPushButton#primaryBtn:focus { border: 1px solid $on_accent; }
QPushButton#newMsgPill:focus { border: 1px solid $on_accent; }
QListWidget#entityList:focus { border: 1px solid $accent; border-radius: 10px; }
"""
