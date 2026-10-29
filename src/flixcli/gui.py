"""RelayDesk desktop client — dark, frameless, three-column.

Presentation layer targets a modern Windows 11 look: frameless window with a
custom title bar, native rounded corners, device avatars, layered surfaces.
All state comes from flixd's loopback API; functional behavior is unchanged.
"""
from __future__ import annotations

import colorsys
import ctypes
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QSize, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QPushButton,
    QScrollArea, QSizePolicy, QStackedWidget, QStatusBar, QTextEdit,
    QVBoxLayout, QWidget,
)

from .icons import icon, logo_icon, logo_pixmap
from .presence import is_online


class ApiError(RuntimeError):
    pass


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
    hues = (212, 174, 160, 152, 262, 200, 24, 320)
    color = QColor()
    # setHsl uses 0-255 component ranges; QSS-style "hsl()" strings are NOT
    # parsed by QColor and silently fall back to black.
    color.setHsl(hues[sum(key.encode()) % len(hues)], 185, 122)
    return color


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
        self.setFixedHeight(44)
        self.setObjectName("titleBar")

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 0, 0, 0)
        row.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(logo_pixmap(26))
        name = QLabel("RelayDesk")
        name.setObjectName("titleName")
        row.addWidget(logo)
        row.addWidget(name)
        row.addStretch()

        self.btn_min = self._win_button("min", self._minimize)
        self.btn_max = self._win_button("max", self._toggle_max)
        self.btn_close = self._win_button("close", self._close, danger=True)
        for b in (self.btn_min, self.btn_max, self.btn_close):
            row.addWidget(b)

    def _win_button(self, icon_name: str, fn, danger: bool = False) -> QPushButton:
        b = QPushButton()
        b.setObjectName("closeBtn" if danger else "winBtn")
        b.setIcon(icon(icon_name, "#93a4bd", 15))
        b.setIconSize(QSize(15, 15))
        b.setFixedSize(46, 44)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(fn)
        return b

    def _minimize(self):
        self.window.showMinimized()

    def _toggle_max(self):
        if self.window.isMaximized():
            self.window.showNormal()
            self.btn_max.setIcon(icon("max", "#93a4bd", 15))
        else:
            self.window.showMaximized()
            self.btn_max.setIcon(icon("restore", "#93a4bd", 15))

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
        self.setWindowTitle("RelayDesk")
        self.resize(1280, 840)
        self.setMinimumSize(980, 640)
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

    def _build(self):
        central = QWidget()
        central.setObjectName("rootWindow")
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.title_bar = TitleBar(self)
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
        self.theme = self._load_theme()
        self.apply_theme(self.theme)

    def _build_rail(self):
        rail = QFrame()
        rail.setObjectName("rail")
        rail.setFixedWidth(72)
        box = QVBoxLayout(rail)
        box.setContentsMargins(12, 16, 12, 14)
        box.setSpacing(8)
        logo = QLabel()
        logo.setPixmap(logo_pixmap(42))
        box.addWidget(logo, 0, Qt.AlignHCenter)
        box.addSpacing(10)
        self.rail_buttons = []
        rail_normal, rail_active = "#8291a8", "#ffffff"
        for name, tip, mode in (
            ("devices", "设备会话", "devices"), ("search", "搜索", "search"),
            ("groups", "群组", "groups"), ("transfers", "传输任务", "transfers"),
            ("android", "Android / ADB", "adb"), ("settings", "设置与状态", "settings"),
        ):
            button = QPushButton()
            button.setObjectName("railButton")
            button.setToolTip(tip)
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setFixedSize(46, 46)
            button.setIcon(icon(name, rail_normal, 21))
            button.setIconSize(QSize(21, 21))
            button.clicked.connect(lambda _checked=False, m=mode: self.set_mode(m))
            button.setProperty("mode", mode)
            button.toggled.connect(lambda on, b=button, n=name:
                                   b.setIcon(icon(n, rail_active if on else rail_normal, 21)))
            self.rail_buttons.append(button)
            box.addWidget(button, 0, Qt.AlignHCenter)
        box.addStretch()
        self.connection_dot = QLabel("●")
        self.connection_dot.setObjectName("connectionDot")
        self.connection_dot.setAlignment(Qt.AlignCenter)
        box.addWidget(self.connection_dot)
        return rail

    def _build_sidebar(self):
        panel = QFrame()
        panel.setObjectName("sidebar")
        panel.setFixedWidth(330)
        box = QVBoxLayout(panel)
        box.setContentsMargins(18, 18, 16, 12)
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
        refresh.setFixedSize(36, 36)
        refresh.setCursor(Qt.PointingHandCursor)
        refresh.clicked.connect(lambda: (self.refresh(), toast_hint(self, "已刷新")))
        add = QPushButton("＋")
        add.setObjectName("roundBtn")
        add.setToolTip("新建群组 / 添加设备")
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
        self.offline_title = QLabel("未发现 flixcli 服务")
        self.offline_title.setObjectName("emptyTitle")
        self.offline_title.setAlignment(Qt.AlignCenter)
        box.addWidget(self.offline_title)
        self.offline_detail = QLabel(
            "默认端口 8898 可能被官方 Flix 占用。\n"
            "可一键启动本机服务（端口 18991/18998，与官方共存），或自行运行 flixd。")
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
        """Start flixd on 18991/18998 (official Flix keeps 8891/8898) detached."""
        import os
        import subprocess
        appdata = os.environ.get("APPDATA", str(Path.home()))
        log_dir = Path(appdata) / "flixcli"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = open(log_dir / "flixd-gui-started.log", "ab")
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
        # A frozen RelayDesk.exe is windowed and does not implement
        # "-m flixcli.cli", so re-spawning itself would fail silently with no
        # console to show why. Use the packaged flixd.exe beside it; only a
        # source checkout falls back to the module form.
        sibling = Path(sys.executable).with_name("flixd.exe")
        if getattr(sys, "frozen", False) and sibling.exists():
            cmd = [str(sibling)]
        else:
            cmd = [sys.executable, "-m", "flixcli.cli", "serve"]
        subprocess.Popen(
            cmd + ["--device-port", "18991", "--api-port", "18998"],
            stdout=log_file, stderr=log_file,
            creationflags=flags, close_fds=True)
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
        header.setFixedHeight(78)
        row = QHBoxLayout(header)
        row.setContentsMargins(20, 10, 20, 10)
        row.setSpacing(12)
        self.session_avatar = Avatar("◉", "relaydesk", 44)
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
        self.session_search.setFixedWidth(200)
        self.session_search.textChanged.connect(lambda _t: self.render_timeline())
        row.addWidget(self.session_search)
        self.header_actions = QHBoxLayout()
        self.header_actions.setSpacing(8)
        row.addLayout(self.header_actions)
        for glyph, tip, fn, icon_name in (
            ("▣", "发送文件", self.choose_files, "file"),
            ("⧉", "发送剪贴板文本", self.send_clipboard, "clipboard"),
            ("⌁", "请求远程观看", self.request_remote, "remote"),
            ("⋯", "更多操作", self.show_more, "more"),
        ):
            button = QPushButton()
            button.setObjectName("headerBtn")
            button.setToolTip(tip)
            button.setFixedSize(40, 40)
            button.setIcon(icon(icon_name, "#9fb0c8", 18))
            button.setIconSize(QSize(18, 18))
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(fn)
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
        box.setContentsMargins(18, 12, 18, 16)
        box.setSpacing(0)
        day_holder = QVBoxLayout()
        self.timeline = QScrollArea()
        self.timeline.setObjectName("timeline")
        self.timeline.setWidgetResizable(True)
        self.timeline_widget = QWidget()
        self.timeline_widget.setObjectName("timelineCanvas")
        self.timeline_layout = QVBoxLayout(self.timeline_widget)
        self.timeline_layout.setContentsMargins(8, 14, 8, 14)
        self.timeline_layout.setSpacing(8)
        self.timeline_layout.addStretch()
        self.timeline.setWidget(self.timeline_widget)
        box.addWidget(self.timeline, 1)
        composer = QFrame()
        composer.setObjectName("composer")
        cbox = QVBoxLayout(composer)
        cbox.setContentsMargins(14, 12, 14, 10)
        self.composer = QTextEdit()
        self.composer.setObjectName("composerEdit")
        self.composer.setPlaceholderText("输入消息，Ctrl+Enter 发送")
        self.composer.setMaximumHeight(96)
        self.composer.installEventFilter(self)
        cbox.addWidget(self.composer)
        actions = QHBoxLayout()
        actions.setSpacing(6)
        image = QPushButton()
        image.setObjectName("chipBtn")
        image.setToolTip("发送图片")
        image.setFixedSize(36, 36)
        image.setIcon(icon("image", "#9fb0c8", 17))
        image.setIconSize(QSize(17, 17))
        image.setCursor(Qt.PointingHandCursor)
        image.clicked.connect(lambda: self.choose_files(images_only=True))
        file_button = QPushButton()
        file_button.setObjectName("chipBtn")
        file_button.setToolTip("发送文件")
        file_button.setFixedSize(36, 36)
        file_button.setIcon(icon("file", "#9fb0c8", 17))
        file_button.setIconSize(QSize(17, 17))
        file_button.setCursor(Qt.PointingHandCursor)
        file_button.clicked.connect(self.choose_files)
        actions.addWidget(image)
        actions.addWidget(file_button)
        self.composer_hint = QLabel("文本与附件经本机 flixd 发送")
        self.composer_hint.setObjectName("muted")
        actions.addSpacing(6)
        actions.addWidget(self.composer_hint)
        actions.addStretch()
        self.send_button = QPushButton(" 发送")
        self.send_button.setObjectName("sendButton")
        self.send_button.setIcon(icon("send", "#ffffff", 15))
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
        self.copy_button.clicked.connect(self.copy_settings_info)
        self.open_dir_button = QPushButton("打开下载目录")
        self.open_dir_button.clicked.connect(self.open_save_dir)
        self.theme_button = QPushButton()
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
        self.update()
        self.repaint()
        self._save_prefs(theme=self.theme)

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

    def open_save_dir(self):
        # The daemon only reports a saveDir when it was configured with one;
        # fall back to the OS Downloads folder the app writes to by default.
        configured = getattr(self, "save_dir", "")
        target = configured if configured and Path(configured).is_dir() else self.default_save_dir()
        if not target or not Path(target).is_dir():
            self.statusBar().showMessage(f"目录不存在：{target or '—'}", 5000)
            return
        try:
            os.startfile(target)
        except OSError as exc:
            self.statusBar().showMessage(f"打不开目录：{exc}", 4000)

    def _toggle_notify(self, on: bool):
        self._save_prefs(notify=bool(on))

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
        self.connection_dot.setProperty("online", True)
        self.connection_dot.style().unpolish(self.connection_dot)
        self.connection_dot.style().polish(self.connection_dot)
        devices = payload.get("devices") or []
        adb = (payload.get("adb") or {}).get("devices", [])
        self.sidebar_footer.setText(
            f"{len(devices)} 台设备 · {len(payload.get('groups') or [])} 个群组 · {len(adb)} 条 ADB 连接")
        self.save_dir = (payload.get("settings") or {}).get("saveDir") or ""
        self.settings_text.setText(
            f"本机别名：{(payload.get('self') or {}).get('alias', '—')}\n"
            f"指纹：{self.self_id}\n"
            f"控制台端口：http://127.0.0.1:{self.api_port}\n"
            f"设备通道端口：{(payload.get('self') or {}).get('port', '—')}\n"
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
        self.render_current()

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
        while self.timeline_layout.count() > 1:
            item = self.timeline_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not self.current:
            return
        kind, key = self.current["kind"], self.current["key"]
        messages = []
        for bubble in reversed(self.payload.get("bubbles") or []):
            if kind == "device" and key not in (bubble.get("from"), bubble.get("to")):
                continue
            if kind == "group" and bubble.get("groupId") != key:
                continue
            messages.append(bubble)
        query = getattr(self, "session_search", None)
        query = query.text().strip() if query is not None else ""
        if query:
            needle = query.lower()
            messages = [b for b in messages
                        if needle in self._search_text(b).lower()]
        if not messages:
            empty = QLabel("没有匹配的消息" if query
                           else "还没有消息。发送一条文本或文件开始会话。")
            empty.setObjectName("muted"); empty.setAlignment(Qt.AlignCenter)
            self.timeline_layout.insertWidget(0, empty)
        last_day = None
        for bubble in messages:
            ts = datetime.fromtimestamp((bubble.get("time") or 0) / 1000)
            if ts.date() != last_day:
                last_day = ts.date()
                sep = QLabel(ts.strftime("%m 月 %d 日"))
                sep.setObjectName("daySeparator")
                sep.setAlignment(Qt.AlignCenter)
                self.timeline_layout.insertWidget(self.timeline_layout.count() - 1, sep)
            raw = bubble.get("content", "")
            outgoing = bubble.get("from") == self.self_id
            wrap = QWidget(); line = QHBoxLayout(wrap); line.setContentsMargins(4, 0, 4, 0)
            image = self._image_path(raw) if isinstance(raw, dict) else ""
            if image:
                widget = self._image_bubble(image, outgoing, ts)
            elif isinstance(raw, dict):
                widget = self._file_bubble(raw, outgoing, ts)
            else:
                widget = self._bubble(str(raw), outgoing, ts)
            if outgoing:
                line.addStretch()
            line.addWidget(widget)
            if not outgoing:
                line.addStretch()
            self.timeline_layout.insertWidget(self.timeline_layout.count() - 1, wrap)
        QTimer.singleShot(0, lambda: self.timeline.verticalScrollBar().setValue(
            self.timeline.verticalScrollBar().maximum()))

    IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic"}

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

    def _image_bubble(self, path: str, outgoing: bool, ts: datetime) -> QFrame:
        frame = QFrame()
        frame.setObjectName("bubbleOut" if outgoing else "bubbleIn")
        box = QVBoxLayout(frame)
        box.setContentsMargins(6, 6, 6, 6)
        picture = QLabel()
        pixmap = QPixmap(path)
        if pixmap.isNull():
            picture.setText("图片无法解码")
            picture.setObjectName("bubbleText")
        else:
            picture.setPixmap(pixmap.scaled(260, 260, Qt.KeepAspectRatio,
                                            Qt.SmoothTransformation))
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
        name = QLabel("▤  " + str(content.get("name") or "文件"))
        name.setObjectName("bubbleText")
        name.setWordWrap(True)
        box.addWidget(name)
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
            os.startfile(path)
        except OSError as exc:
            self.statusBar().showMessage(f"打不开：{exc}", 4000)

    def _bubble(self, content: str, outgoing: bool, ts: datetime) -> QFrame:
        frame = QFrame()
        frame.setObjectName("bubbleOut" if outgoing else "bubbleIn")
        box = QVBoxLayout(frame)
        box.setContentsMargins(14, 9, 14, 7)
        text = QLabel(content)
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextSelectableByMouse)
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
        if (self.current or {}).get("kind") == "group":
            menu.addAction("群设置…", self.group_settings_dialog)
            menu.addSeparator()
        menu.addAction("刷新会话", self.refresh)
        menu.addAction("查看传输任务", lambda: self.set_mode("transfers"))
        menu.addAction("设置与状态", lambda: self.set_mode("settings"))
        menu.exec(self.cursor().pos())

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
        self.workspace_stack.setCurrentIndex(6)
        self.statusBar().showMessage(
            "未发现 flixcli 服务——可从下方一键启动（与官方 Flix 端口共存）")

    def eventFilter(self, watched, event):
        if watched is self.composer and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter) and event.modifiers() == Qt.ControlModifier:
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


def palette_for(theme: str) -> QPalette:
    """Dark/light base palette for the parts QSS does not cover."""
    light = theme == "light"
    text = QColor("#12161d") if light else QColor("#eef3fc")
    base = QColor("#ffffff") if light else QColor("#0d1119")
    window = QColor("#f4f6fa") if light else QColor("#080b10")
    muted = QColor("#5c6b80") if light else QColor("#9fb0c8")
    pal = QPalette()
    pal.setColor(QPalette.Window, window)
    pal.setColor(QPalette.WindowText, text)
    pal.setColor(QPalette.Base, base)
    pal.setColor(QPalette.AlternateBase, window)
    pal.setColor(QPalette.Text, text)
    pal.setColor(QPalette.Button, window)
    pal.setColor(QPalette.ButtonText, text)
    pal.setColor(QPalette.ToolTipBase, base)
    pal.setColor(QPalette.ToolTipText, text)
    pal.setColor(QPalette.PlaceholderText, muted)
    pal.setColor(QPalette.Highlight, QColor("#3b82f6"))
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    return pal


# Brand colours keep their hue in both themes; neutrals are inverted.
LIGHT_KEEP = {"#3b82f6", "#2563eb", "#8b5cf6", "#4f46e5", "#21c78a",
              "#ef5350", "#e81123", "#ffffff", "#000000"}

HEX_RE = re.compile(r"#[0-9a-fA-F]{6}")


RGBA_RE = re.compile(r"rgba\((\d{1,3}),\s*(\d{1,3}),\s*(\d{1,3}),\s*([0-9.]+)\)")


def _flip_rgb(r: int, g: int, b: int):
    """Mirror a colour's lightness so the whole sheet follows the theme.

    Foreground colours (the light ones) are pushed deep into the dark range
    rather than merely mirrored: a mid-grey on a near-white background reads
    worse than the original pair did, so light text lands around L=0.12-0.30.
    """
    hue, light, sat = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    if light >= 0.5:
        light = 0.12 + (1.0 - light) * 0.30
    else:
        light = min(1.0 - light, 0.93)
    return tuple(round(c * 255) for c in colorsys.hls_to_rgb(hue, light, sat))


def _flip_hex(value: str) -> str:
    r, g, b = (int(value[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % _flip_rgb(r, g, b)


def _flip_rgba(match) -> str:
    r, g, b = _flip_rgb(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return "rgba(%d,%d,%d,%s)" % (r, g, b, match.group(4))


# The flip turns `QLabel#bubbleText` dark, which is right for incoming bubbles
# but wrong on the blue outgoing bubble: pin white there explicitly.
LIGHT_OVERRIDES = """
QFrame#bubbleOut QLabel#bubbleText { color: #ffffff; }
QFrame#bubbleOut QLabel#bubbleMeta { color: rgba(255,255,255,0.80); }
QFrame#bubbleIn { background: #ffffff; }
QLabel#bubbleMeta { color: #5c6b80; }
"""


BRAND_RE = re.compile(r"#(3b82f6|2563eb|8b5cf6|4f46e5|21c78a|ef5350|e81123)", re.I)


def _flip_named(block: str) -> str:
    """`color: white` is not a hex, so it survived every flip: on a light
    background that is white-on-white. Rules that carry a brand colour keep
    their white text (accent buttons, badges)."""
    if BRAND_RE.search(block):
        return block
    return block.replace("color: white", "color: #12161d")


def styles_for(theme: str) -> str:
    """Dark is the authored sheet; light is the same sheet with flipped neutrals."""
    if theme != "light":
        return STYLES
    out = HEX_RE.sub(
        lambda m: m.group(0) if m.group(0).lower() in LIGHT_KEEP else _flip_hex(m.group(0)),
        STYLES)
    # rgba() carries the translucent headers and overlays; hex-only flipping
    # left those dark (the session header was the visible offender).
    out = RGBA_RE.sub(_flip_rgba, out)
    # Named colours go last, otherwise the dark text they produce is flipped
    # straight back to light by the hex pass above.
    out = "".join(_flip_named(b) for b in re.split(r"(?<=\})", out))
    return out + LIGHT_OVERRIDES


LIGHT_SWAP = {
    "#080b10": "#eef1f6", "#0b0f16": "#f4f6fa", "#0d1119": "#ffffff",
    "#1a212c": "#ffffff", "#232c3a": "#e4e9f1", "#232c39": "#e9edf4",
    "#242e3d": "#eceff5", "#2b3547": "#dbe2ec",
    "#eef3fc": "#12161d", "#edf3ff": "#12161d", "#e5edf9": "#1b2230",
    "#dce6f6": "#243044", "#dbe5f5": "#1b2230", "#c8d4e7": "#2a3446",
    "#9fb0c8": "#5c6b80", "#93a4bd": "#65758c", "#7c8ba3": "#6b7a90",
}

STYLES = """
QWidget#rootWindow, QWidget#page { background: qlineargradient(y1:0,y1:1,
    stop:0 #0b0f16, stop:1 #080b10); color: #eef3fc; }
QFrame#titleBar { background: #0d1119; border-bottom: 1px solid #232c3a; }
QLabel#titleLogo { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
    stop:0 #3b82f6, stop:1 #8b5cf6); border-radius: 7px; color: white;
    font-size: 14px; font-weight: 800; }
QLabel#titleName { color: #dbe5f5; font-size: 13px; font-weight: 650; }
QPushButton#winBtn { background: transparent; color: #93a4bd; border: 0;
    font-size: 13px; border-radius: 0; }
QPushButton#winBtn:hover { background: #232c39; color: white; }
QPushButton#closeBtn { background: transparent; color: #93a4bd; border: 0;
    font-size: 13px; border-radius: 0; }
QPushButton#closeBtn:hover { background: #e81123; color: white; }

QFrame#rail { background: #0d1119; border-right: 1px solid #232c3a; }
QLabel#logo { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
    stop:0 #3b82f6, stop:1 #8b5cf6); border-radius: 12px; color: white;
    font-size: 21px; font-weight: 800; }
QPushButton#railButton { background: transparent; color: #8291a8; border: 0;
    border-radius: 13px; font-size: 19px; }
QPushButton#railButton:hover { background: #1c2430; color: #dbe5f5; }
QPushButton#railButton:checked { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
    stop:0 #2563eb, stop:1 #4f46e5); color: white; }
QLabel#connectionDot { color: #ef5350; font-size: 16px; }
QLabel#connectionDot[online="true"] { color: #21c78a; }

QFrame#sidebar { background: #0a0d13; border-right: 1px solid #222b39; }
QLabel#brand { color: white; font-size: 23px; font-weight: 800; letter-spacing: .2px; }
QLabel#brandSub { color: #6e7d95; font-size: 11.5px; }
QPushButton#roundBtn { background: #1a212c; color: #9fb0c8; border: 1px solid #2b3547;
    border-radius: 18px; font-size: 15px; }
QPushButton#roundBtn:hover { background: #242e3d; color: white; border-color: #3b82f6; }
QLineEdit#search { background: #141924; color: #e5edf9; border: 1px solid #29323f;
    border-radius: 10px; padding: 10px 13px; font-size: 13px; selection-background-color: #2563eb; }
QLineEdit#search:focus { border-color: #3b82f6; background: #171d29; }
QLabel#sidebarTitle { color: #7f8da4; padding-top: 4px; font-weight: 700; font-size: 12px;
    letter-spacing: 1.5px; }
QListWidget#entityList { background: transparent; border: 0; outline: 0; }
QListWidget#entityList::item { color: #dbe5f5; margin: 3px 0; border-radius: 13px;
    border: 1px solid transparent; }
QListWidget#entityList::item:selected { background: #20222b; border: 1px solid #3b82f6; }
QListWidget#entityList::item:hover { background: #171c25; }
QFrame#entityCard { background: transparent; }
QLabel#entityName { color: #eef3fc; font-size: 13.5px; font-weight: 650; }
QLabel#entitySub { color: #7c8ba3; font-size: 11.5px; }
QLabel#onlineDot { color: #21c78a; font-size: 11px; }
QLabel#offlineDot { color: #55627a; font-size: 11px; }

QFrame#workspace { background: transparent; }
QFrame#sessionHeader { background: rgba(14,18,25,0.85); border-bottom: 1px solid #232c3a; }
QLabel#sessionTitle { color: white; font-size: 17px; font-weight: 750; }
QLabel#muted { color: #7c8ba3; font-size: 12px; }
QPushButton#headerBtn { background: #1a212c; color: #9fb0c8; border: 1px solid #2b3547;
    border-radius: 20px; font-size: 16px; }
QPushButton#headerBtn:hover { background: #243044; color: white; border-color: #3b82f6; }

QScrollArea#timeline { background: transparent; border: 0; }
QWidget#timelineCanvas { background: transparent; }
QLabel#daySeparator { color: #5b6980; font-size: 11px; background: #171d27;
    border-radius: 9px; padding: 3px 12px; margin: 4px auto; }
QFrame#bubbleOut { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
    stop:0 #2f6ef2, stop:1 #2b5fe0); border-radius: 15px; border-bottom-right-radius: 5px; }
QFrame#bubbleIn { background: #222834; border-radius: 15px; border-bottom-left-radius: 5px; }
QLabel#bubbleText { color: white; font-size: 13.5px; line-height: 150%; }
QLabel#bubbleMeta { color: rgba(255,255,255,0.62); font-size: 10.5px; }
QFrame#bubbleIn QLabel#bubbleMeta { color: #7c8ba3; }

QFrame#composer { background: #161b24; border: 1px solid #2b3543; border-radius: 16px; }
QTextEdit#composerEdit { background: transparent; border: 0; color: white;
    font-size: 13.5px; selection-background-color: #2563eb; }
QPushButton#chipBtn { background: #212936; color: #9fb0c8; border: 1px solid #2e3949;
    border-radius: 10px; font-size: 15px; }
QPushButton#chipBtn:hover { color: white; border-color: #3b82f6; }
QPushButton#sendButton { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
    stop:0 #2563eb, stop:1 #4f46e5); color: white; border: 0; border-radius: 18px;
    padding: 9px 22px; font-weight: 700; font-size: 13px; }
QPushButton#sendButton:hover { background: #3b82f6; }
QPushButton#sendButton:pressed { background: #1d4ed8; }

QListWidget#contentList { background: #10151d; color: #dce6f6; border: 1px solid #242e3d;
    border-radius: 11px; padding: 6px; }
QListWidget#contentList::item { padding: 10px; border-bottom: 1px solid #1d2531;
    border-radius: 6px; }
QListWidget#contentList::item:hover { background: #181f2a; }
QLabel#pageTitle { color: white; font-size: 22px; font-weight: 800; }
QLabel#emptyLogo { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
    stop:0 #3b82f6, stop:1 #8b5cf6); border-radius: 30px; color: white;
    font-size: 46px; font-weight: 800; }
QFrame#settingsCard { background: #121824; border: 1px solid #263042; border-radius: 13px; }
QLabel#settingsText { color: #b9c7da; font-size: 13px; }
QLabel#mediaPreview { background: #05070a; color: #718096; border: 1px solid #263143;
    border-radius: 11px; }
QCheckBox { color: #c8d4e7; spacing: 7px; font-size: 13px; }
QPushButton#primaryBtn { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
    stop:0 #2563eb, stop:1 #4f46e5); color: white; border: 0; border-radius: 9px;
    padding: 9px 18px; font-weight: 700; }
QPushButton#primaryBtn:hover { background: #3b82f6; }
QPushButton#secondaryButton { background: #202735; color: #edf3ff; border: 1px solid #35405a;
    border-radius: 9px; padding: 8px 14px; font-size: 12.5px; }
QPushButton#secondaryButton:hover { border-color: #3b82f6; background: #28324a; }
QPushButton#secondaryButton:disabled { color: #596579; background: #171b22; border-color: #232c39; }
QMenu { background: #161c26; color: white; border: 1px solid #354055; border-radius: 9px;
    padding: 5px; }
QMenu::item { padding: 8px 22px; border-radius: 6px; }
QMenu::item:selected { background: #2563eb; }
QStatusBar { background: #0d1119; color: #7f8ea6; border-top: 1px solid #1e2734;
    font-size: 11.5px; }
QScrollBar:vertical { background: transparent; width: 9px; margin: 2px; }
QScrollBar::handle:vertical { background: #313e52; border-radius: 4px; min-height: 36px; }
QScrollBar::handle:vertical:hover { background: #40506a; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
"""
