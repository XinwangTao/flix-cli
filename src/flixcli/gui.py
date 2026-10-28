"""Dark three-column desktop client for the flixd loopback API."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QSize, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QPushButton, QScrollArea,
    QSizePolicy, QSplitter, QStackedWidget, QStatusBar, QTextEdit, QVBoxLayout,
    QWidget,
)


class ApiError(RuntimeError):
    pass


class LocalApi:
    def __init__(self, port: int = 8898):
        self.base = f"http://127.0.0.1:{port}"

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


class IconButton(QPushButton):
    def __init__(self, glyph: str, tooltip: str, parent=None, *, checkable=False):
        super().__init__(glyph, parent)
        self.setObjectName("iconButton")
        self.setToolTip(tooltip)
        self.setCheckable(checkable)
        self.setFixedSize(42, 42)
        self.setCursor(Qt.PointingHandCursor)


class DeviceItem(QListWidgetItem):
    def __init__(self, title: str, subtitle: str, key: str, kind: str,
                 online: bool = True):
        marker = "●" if online else "○"
        super().__init__(f"{marker}  {title}\n     {subtitle}")
        self.setData(Qt.UserRole, {"key": key, "kind": kind})
        self.setToolTip(f"{title}\n{subtitle}")
        self.setSizeHint(QSize(270, 68))


class Bubble(QFrame):
    def __init__(self, content: str, outgoing: bool, meta: str = ""):
        super().__init__()
        self.setObjectName("bubbleOut" if outgoing else "bubbleIn")
        box = QVBoxLayout(self)
        box.setContentsMargins(15, 11, 15, 9)
        text = QLabel(content)
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        text.setObjectName("bubbleText")
        box.addWidget(text)
        if meta:
            stamp = QLabel(meta)
            stamp.setObjectName("bubbleMeta")
            stamp.setAlignment(Qt.AlignRight)
            box.addWidget(stamp)
        self.setMaximumWidth(610)


class MainWindow(QMainWindow):
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
        self.setWindowTitle("RelayDesk")
        self.resize(1240, 820)
        self.setMinimumSize(940, 620)
        self._build()
        self.refresh()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2500)

    def _build(self):
        root = QWidget()
        root.setObjectName("root")
        row = QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self._build_rail())
        row.addWidget(self._build_sidebar())
        row.addWidget(self._build_workspace(), 1)
        self.setCentralWidget(root)
        status = QStatusBar()
        status.setSizeGripEnabled(False)
        self.setStatusBar(status)
        self.statusBar().showMessage("正在连接本机服务…")
        self.setStyleSheet(STYLES)

    def _build_rail(self):
        rail = QFrame()
        rail.setObjectName("rail")
        rail.setFixedWidth(68)
        box = QVBoxLayout(rail)
        box.setContentsMargins(10, 14, 10, 12)
        box.setSpacing(10)
        logo = QLabel("R")
        logo.setObjectName("logo")
        logo.setAlignment(Qt.AlignCenter)
        logo.setFixedSize(44, 44)
        box.addWidget(logo)
        self.rail_buttons = []
        for glyph, tip, mode in (
            ("↯", "设备会话", "devices"), ("⌕", "搜索消息和设备", "search"),
            ("#", "群组", "groups"), ("⇅", "传输任务", "transfers"),
            ("A", "Android / ADB", "adb"), ("⚙", "设置与状态", "settings"),
        ):
            button = IconButton(glyph, tip, checkable=True)
            button.clicked.connect(lambda _checked=False, m=mode: self.set_mode(m))
            button.setProperty("mode", mode)
            self.rail_buttons.append(button)
            box.addWidget(button, 0, Qt.AlignHCenter)
        box.addStretch()
        self.connection_dot = QLabel("●")
        self.connection_dot.setObjectName("connectionDot")
        self.connection_dot.setToolTip("本机服务状态")
        self.connection_dot.setAlignment(Qt.AlignCenter)
        box.addWidget(self.connection_dot)
        return rail

    def _build_sidebar(self):
        panel = QFrame()
        panel.setObjectName("sidebar")
        panel.setFixedWidth(318)
        box = QVBoxLayout(panel)
        box.setContentsMargins(16, 16, 14, 12)
        title_row = QHBoxLayout()
        brand = QLabel("RelayDesk")
        brand.setObjectName("brand")
        title_row.addWidget(brand)
        title_row.addStretch()
        refresh = IconButton("↻", "刷新")
        refresh.clicked.connect(self.refresh)
        add = IconButton("+", "添加或配对设备")
        add.clicked.connect(lambda: self.statusBar().showMessage(
            "使用 flix pair <ip:port> 配对，GUI 配对向导将在下一步完善"))
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
        box.addWidget(self.workspace_stack, 1)
        return area

    def _build_header(self):
        header = QFrame()
        header.setObjectName("sessionHeader")
        header.setFixedHeight(72)
        row = QHBoxLayout(header)
        row.setContentsMargins(18, 10, 18, 10)
        self.session_icon = QLabel("◉")
        self.session_icon.setObjectName("sessionIcon")
        self.session_icon.setAlignment(Qt.AlignCenter)
        self.session_icon.setFixedSize(44, 44)
        row.addWidget(self.session_icon)
        labels = QVBoxLayout()
        self.session_title = QLabel("选择设备或群组")
        self.session_title.setObjectName("sessionTitle")
        self.session_subtitle = QLabel("从左侧列表开始")
        self.session_subtitle.setObjectName("muted")
        labels.addWidget(self.session_title)
        labels.addWidget(self.session_subtitle)
        row.addLayout(labels)
        row.addStretch()
        for glyph, tip, fn in (
            ("▣", "发送文件", self.choose_files),
            ("▤", "发送剪贴板文本", self.send_clipboard),
            ("⌁", "请求远程观看", self.request_remote),
            ("⋯", "更多操作", self.show_more),
        ):
            button = IconButton(glyph, tip)
            button.clicked.connect(fn)
            row.addWidget(button)
        return header

    def _build_empty(self):
        page = QWidget()
        page.setObjectName("page")
        box = QVBoxLayout(page)
        box.addStretch()
        icon = QLabel("↯")
        icon.setObjectName("emptyIcon")
        icon.setAlignment(Qt.AlignCenter)
        box.addWidget(icon)
        title = QLabel("选择一台设备开始协作")
        title.setObjectName("emptyTitle")
        title.setAlignment(Qt.AlignCenter)
        box.addWidget(title)
        note = QLabel("发送文本、图片、文件或发起经过批准的远程会话")
        note.setObjectName("muted")
        note.setAlignment(Qt.AlignCenter)
        box.addWidget(note)
        box.addStretch()
        return page

    def _build_chat(self):
        page = QWidget()
        page.setObjectName("page")
        box = QVBoxLayout(page)
        box.setContentsMargins(16, 10, 16, 14)
        self.timeline = QScrollArea()
        self.timeline.setObjectName("timeline")
        self.timeline.setWidgetResizable(True)
        self.timeline_widget = QWidget()
        self.timeline_layout = QVBoxLayout(self.timeline_widget)
        self.timeline_layout.setContentsMargins(4, 10, 4, 10)
        self.timeline_layout.setSpacing(10)
        self.timeline_layout.addStretch()
        self.timeline.setWidget(self.timeline_widget)
        box.addWidget(self.timeline, 1)
        composer = QFrame()
        composer.setObjectName("composer")
        cbox = QVBoxLayout(composer)
        cbox.setContentsMargins(12, 10, 12, 9)
        self.composer = QTextEdit()
        self.composer.setObjectName("composerEdit")
        self.composer.setPlaceholderText("输入消息，Ctrl+Enter 发送")
        self.composer.setMaximumHeight(92)
        self.composer.installEventFilter(self)
        cbox.addWidget(self.composer)
        actions = QHBoxLayout()
        image = IconButton("▧", "发送图片")
        image.clicked.connect(lambda: self.choose_files(images_only=True))
        file_button = IconButton("▤", "发送文件")
        file_button.clicked.connect(self.choose_files)
        actions.addWidget(image)
        actions.addWidget(file_button)
        self.composer_hint = QLabel("文本与附件通过本机 flixd 发送")
        self.composer_hint.setObjectName("muted")
        actions.addWidget(self.composer_hint)
        actions.addStretch()
        self.send_button = QPushButton("发送  ➤")
        self.send_button.setObjectName("sendButton")
        self.send_button.clicked.connect(self.send_message)
        actions.addWidget(self.send_button)
        cbox.addLayout(actions)
        box.addWidget(composer)
        return page

    def _build_transfers(self):
        page = QWidget(); page.setObjectName("page")
        box = QVBoxLayout(page); box.setContentsMargins(24, 20, 24, 20)
        title = QLabel("传输任务"); title.setObjectName("pageTitle"); box.addWidget(title)
        self.transfer_list = QListWidget(); self.transfer_list.setObjectName("contentList")
        box.addWidget(self.transfer_list, 1)
        return page

    def _build_adb(self):
        page = QWidget(); page.setObjectName("page")
        box = QVBoxLayout(page); box.setContentsMargins(24, 20, 24, 20)
        title = QLabel("Android / ADB"); title.setObjectName("pageTitle"); box.addWidget(title)
        note = QLabel("显示 USB / 无线序列号和授权状态；文件操作仍要求显式目标。")
        note.setObjectName("muted"); box.addWidget(note)
        self.adb_list = QListWidget(); self.adb_list.setObjectName("contentList")
        box.addWidget(self.adb_list, 1)
        return page

    def _build_settings(self):
        page = QWidget(); page.setObjectName("page")
        box = QVBoxLayout(page); box.setContentsMargins(24, 20, 24, 20)
        title = QLabel("设置与状态"); title.setObjectName("pageTitle"); box.addWidget(title)
        self.settings_text = QLabel("等待服务配置")
        self.settings_text.setObjectName("settingsText")
        self.settings_text.setWordWrap(True)
        box.addWidget(self.settings_text)
        box.addStretch()
        return page

    def _build_remote(self):
        page = QWidget(); page.setObjectName("page")
        box = QVBoxLayout(page); box.setContentsMargins(20, 16, 20, 16)
        row = QHBoxLayout()
        self.remote_status = QLabel("默认仅观看；控制必须由对端单独批准")
        self.remote_status.setObjectName("muted")
        row.addWidget(self.remote_status)
        row.addStretch()
        self.remote_control = QCheckBox("请求控制")
        row.addWidget(self.remote_control)
        self.remote_request = QPushButton("请求观看")
        self.remote_request.setObjectName("secondaryButton")
        self.remote_request.clicked.connect(self.request_remote)
        row.addWidget(self.remote_request)
        box.addLayout(row)
        self.remote_sessions = QListWidget(); self.remote_sessions.setObjectName("contentList")
        self.remote_sessions.currentItemChanged.connect(lambda _a, _b: self.update_remote_actions())
        box.addWidget(self.remote_sessions)
        actions = QHBoxLayout()
        self.approve_view = QPushButton("批准观看")
        self.approve_control = QPushButton("批准观看 + 控制")
        self.deny_remote = QPushButton("拒绝")
        self.connect_remote = QPushButton("连接")
        self.revoke_remote = QPushButton("断开")
        for button in (self.approve_view, self.approve_control, self.deny_remote,
                       self.connect_remote, self.revoke_remote):
            button.setObjectName("secondaryButton"); actions.addWidget(button)
        self.approve_view.clicked.connect(lambda: self.decide_remote(["view"]))
        self.approve_control.clicked.connect(lambda: self.decide_remote(["view", "control"]))
        self.deny_remote.clicked.connect(self.deny_selected_remote)
        self.connect_remote.clicked.connect(self.connect_selected_remote)
        self.revoke_remote.clicked.connect(self.revoke_selected_remote)
        box.addLayout(actions)
        self.remote_preview = QLabel("等待远端画面")
        self.remote_preview.setObjectName("mediaPreview")
        self.remote_preview.setAlignment(Qt.AlignCenter)
        self.remote_preview.setMinimumHeight(320)
        self.remote_preview.installEventFilter(self)
        box.addWidget(self.remote_preview, 1)
        return page

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

    def refresh(self):
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
        self.settings_text.setText(
            f"本机：{(payload.get('self') or {}).get('alias', '—')}\n"
            f"指纹：{self.self_id}\nAPI：http://127.0.0.1:{self.api_port}\n"
            f"设备端口：{(payload.get('self') or {}).get('port', '—')}\n"
            f"下载目录：{(payload.get('settings') or {}).get('saveDir') or '系统 Downloads'}\n"
            f"媒体会话：{len((payload.get('media') or {}).get('sessions', []))}")
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
                subtitle = f"{device.get('os') or 'unknown'} · {device.get('ip') or '离线'}:{device.get('port') or ''}"
                entities.append((title, subtitle, device.get("fingerprint", ""), "device", bool(device.get("ip"))))
        if mode in ("groups", "search"):
            for group in self.payload.get("groups") or []:
                entities.append((group.get("name") or "未命名群组",
                                 f"{len(group.get('members', []))} 位成员",
                                 group.get("id", ""), "group", True))
        if mode == "adb":
            for device in (self.payload.get("adb") or {}).get("devices", []):
                entities.append((device.get("model") or "Android",
                                 f"{device.get('state')} · {device.get('serial')}",
                                 device.get("serial", ""), "adb", device.get("authorized", False)))
        for title, subtitle, key, kind, online in entities:
            if query and query not in (title + " " + subtitle + " " + key).lower():
                continue
            item = DeviceItem(title, subtitle, key, kind, online)
            self.entity_list.addItem(item)
            if current_data == item.data(Qt.UserRole):
                self.entity_list.setCurrentItem(item)
        self.entity_list.blockSignals(False)

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
            self.session_title.setText(device.get("alias") or key[:8])
            self.session_subtitle.setText(
                f"{device.get('os') or 'unknown'} · {device.get('ip') or '离线'}:{device.get('port') or ''}")
            self.session_icon.setText("▣")
        else:
            group = next((g for g in self.payload.get("groups", []) if g.get("id") == key), {})
            self.session_title.setText(group.get("name") or "群组")
            self.session_subtitle.setText(f"{len(group.get('members', []))} 位成员 · 支持 @指纹前缀")
            self.session_icon.setText("#")
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
        if not messages:
            empty = QLabel("还没有消息。发送一条文本或文件开始会话。")
            empty.setObjectName("muted"); empty.setAlignment(Qt.AlignCenter)
            self.timeline_layout.insertWidget(0, empty)
        for bubble in messages:
            content = bubble.get("content", "")
            if isinstance(content, dict):
                content = (f"▤  {content.get('name', '文件')}\n"
                           f"{self.format_size(content.get('size', 0))} · "
                           f"{float(content.get('progress', 0)) * 100:.0f}%")
            outgoing = bubble.get("from") == self.self_id
            ts = datetime.fromtimestamp((bubble.get("time") or 0) / 1000).strftime("%H:%M")
            wrap = QWidget(); line = QHBoxLayout(wrap); line.setContentsMargins(4, 0, 4, 0)
            if outgoing:
                line.addStretch()
            line.addWidget(Bubble(str(content), outgoing, ts))
            if not outgoing:
                line.addStretch()
            self.timeline_layout.insertWidget(self.timeline_layout.count() - 1, wrap)
        QTimer.singleShot(0, lambda: self.timeline.verticalScrollBar().setValue(
            self.timeline.verticalScrollBar().maximum()))

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
        menu.addAction("刷新会话", self.refresh)
        menu.addAction("查看传输任务", lambda: self.set_mode("transfers"))
        menu.addAction("设置与状态", lambda: self.set_mode("settings"))
        menu.exec(self.cursor().pos())

    def populate_transfers(self):
        self.transfer_list.clear()
        files = [b for b in self.payload.get("bubbles") or [] if isinstance(b.get("content"), dict)]
        for bubble in files:
            data = bubble["content"]
            self.transfer_list.addItem(
                f"▤  {data.get('name', '文件')}\n    {self.format_size(data.get('size', 0))} · "
                f"{float(data.get('progress', 0)) * 100:.0f}%")
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
        self.statusBar().showMessage("无法连接 flixd：" + detail)

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


def main(argv: list[str] | None = None, *, api_port: int = 8898) -> int:
    app = QApplication(argv or sys.argv)
    app.setApplicationName("RelayDesk")
    app.setStyle("Fusion")
    window = MainWindow(api_port)
    window.show()
    return app.exec()


STYLES = """
QWidget#root, QWidget#page { background: #090c12; color: #edf3ff; }
QFrame#rail { background: #0d1119; border-right: 1px solid #242d3b; }
QLabel#logo { background: #2563eb; color: white; border-radius: 12px; font-size: 22px; font-weight: 800; }
QPushButton#iconButton { background: transparent; color: #93a4bd; border: 0; border-radius: 12px; font-size: 20px; }
QPushButton#iconButton:hover { background: #242c39; color: white; }
QPushButton#iconButton:checked { background: #1d4ed8; color: white; }
QLabel#connectionDot { color: #ef5350; font-size: 18px; }
QLabel#connectionDot[online="true"] { color: #21c78a; }
QFrame#sidebar { background: #080a0e; border-right: 1px solid #262f3e; }
QLabel#brand { font-size: 27px; font-weight: 750; color: white; }
QLabel#sidebarTitle { color: #7f8da4; padding-top: 5px; font-weight: 650; }
QLineEdit#search { background: #151a23; color: #e5edf9; border: 1px solid #2b3445; border-radius: 8px; padding: 9px 11px; }
QListWidget#entityList { background: transparent; border: 0; outline: 0; }
QListWidget#entityList::item { color: #dbe5f5; padding: 8px 9px; margin: 3px 0; border-radius: 12px; }
QListWidget#entityList::item:selected { background: #24262c; border: 1px solid #1687ff; color: white; }
QListWidget#entityList::item:hover { background: #171c25; }
QFrame#workspace { background: #090c12; }
QFrame#sessionHeader { background: #0e1219; border-bottom: 1px solid #283142; }
QLabel#sessionIcon { background: #2563eb; border-radius: 12px; color: white; font-size: 21px; }
QLabel#sessionTitle { color: white; font-size: 17px; font-weight: 700; }
QLabel#muted, QLabel#bubbleMeta { color: #8190a8; font-size: 12px; }
QLabel#emptyIcon { color: #347dff; font-size: 58px; }
QLabel#emptyTitle { color: white; font-size: 22px; font-weight: 700; }
QScrollArea#timeline { background: #090c12; border: 0; }
QScrollArea#timeline > QWidget > QWidget { background: #090c12; }
QFrame#bubbleOut { background: #3478f6; border-radius: 12px; }
QFrame#bubbleIn { background: #252a33; border-radius: 12px; }
QLabel#bubbleText { color: white; font-size: 14px; }
QFrame#composer { background: #171b22; border: 1px solid #2d3543; border-radius: 13px; }
QTextEdit#composerEdit { background: transparent; border: 0; color: white; padding: 3px; selection-background-color: #2563eb; }
QPushButton#sendButton { background: #2563eb; color: white; border: 0; border-radius: 18px; padding: 10px 20px; font-weight: 700; }
QPushButton#sendButton:hover { background: #347dff; }
QPushButton#secondaryButton { background: #242a35; color: #edf3ff; border: 1px solid #384457; border-radius: 8px; padding: 9px 14px; }
QPushButton#secondaryButton:hover { border-color: #3b82f6; background: #293345; }
QPushButton#secondaryButton:disabled { color: #596579; background: #171b22; }
QListWidget#contentList { background: #11161e; color: #dce6f6; border: 1px solid #283243; border-radius: 9px; padding: 8px; }
QListWidget#contentList::item { padding: 10px; border-bottom: 1px solid #242d3a; }
QLabel#pageTitle { color: white; font-size: 21px; font-weight: 700; }
QLabel#settingsText { color: #b9c7da; font-size: 14px; line-height: 1.7; }
QLabel#mediaPreview { background: #05070a; color: #718096; border: 1px solid #263143; border-radius: 8px; }
QCheckBox { color: #c8d4e7; spacing: 7px; }
QMenu { background: #171c25; color: white; border: 1px solid #354055; }
QMenu::item:selected { background: #2563eb; }
QStatusBar { background: #0d1119; color: #8392aa; border-top: 1px solid #242d3b; }
QScrollBar:vertical { background: transparent; width: 9px; }
QScrollBar::handle:vertical { background: #354154; border-radius: 4px; min-height: 30px; }
"""
