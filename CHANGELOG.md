# 变更记录

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；版本号与 GitHub Release 标签一致。
可执行产物见 [Releases](https://github.com/XinwangTao/flix-cli/releases)。

## [Unreleased] - 2026-11-02

### 界面
- 桌面控制台改为原生 Windows 外观：系统标题栏、64px 图标导航、280px 会话列表、
  中性灰配色与 4px 圆角；连接状态并入底部状态栏，不再使用独立信号横幅。
- 传输页改为台账表格（方向/对端/大小/进度/状态/操作，40px 行高）；ADB 页按序列号去重、
  中文授权状态、行内复制；设置页改为左对齐属性表，动作收进行内。
- 服务断开时缓存设备显示“未知”并清空陈旧列表，同时隐藏搜索、配对与列表标题；
  错误改为中文原因，原始异常只放在悬停提示。
- 首页默认选中会话；补充 Ctrl+F、列表回车、搜索 Esc 与键盘焦点环。
- 新增可选毛玻璃材质（设置 → 窗口材质）：按 Apple HIG Liquid Glass 只给导航栏、
  会话列表、标题栏和状态栏上材质，内容层保持不透明；模糊约 36px、填充 60–80%、
  带轻度饱和提升，浅色/深色各自适应；系统关闭透明效果时默认退回标准材质。

### 安全
- 收紧局域网信任边界：`/bubble`、剪贴板、rdShell、rdAgent、群快照只接受已配对且来源 IP
  与设备记录一致的发送方；未配对/来源不符返回 403 且不落库。
- 配对必须携带对端 4 位配对码（常量时间比较）；`flix pair` / MCP `flix_pair` 在联网前拒绝无码调用。
- Web 上传文件名只取 basename 并校验最终父目录，阻断 `../../` 路径穿越；Web PIN 所有入口共享限流。
- Desktop API 校验 `Host`/`Origin` 与 JSON Content-Type，阻断 CSRF/DNS rebinding。
- 线日志改为显式 `FLIX_WIRE_LOG` 开启，正文哈希化、PIN 打码、5 MB 轮转。

### 修复
- 同一 `APPDATA` 可启动多个 flixd 并争抢端口/偏好文件：新增单实例锁，第二个实例退出码 5。
- 偏好文件跨进程写入丢键；升级脚本硬编码路径、哈希缺失；发现投毒可改写已配对设备 IP
  （现在先 `ping_v2` 校验身份再接受变更）。
- `flix ui` 跨线程使用 SQLite；MCP `flix_pair` 崩溃；CLI/MCP 建群未推送群快照；shell 参数引号丢失。
- 远端执行输出无上限、超时不杀进程树；outbox 重试沿用旧 `created` 导致刚重试即过期。
- TUN/虚拟网卡被当成局域网接口（现在按默认网关与私网前缀选路，跳过 198.18/100.64/169.254/127）。
- 媒体轨道屏幕抓取可能永久阻塞：单帧抓取 3 秒超时后结束轨道。
- MCP/路由器对 Unicode 数字（`²`.isdigit() 为真但 `int()` 会抛）做端口判断导致崩溃；
  统一改为 ASCII 数字校验。
- 依赖安全升级：`cryptography>=50,<51`、`Pillow>=13,<14`（清除 pip-audit 报告的 46 个已知漏洞）。

### 测试
- 新增 100 轮审计驱动 `scripts/audit_rounds.py`（修复回归、CLI/MCP 矩阵、模糊、A/B 基线、工具链）。
- 新增 `tests/test_fuzz_props.py`（Hypothesis：发现报文、二维码、意图回复、升级脚本转义）。

## [v2.2.1] - 2026-10-30

### 修复
- 打开窗口后终端反复闪烁：GUI 每 2.5s 轮询会触发 `adb devices`，剪贴板同步会拉起 `clip.exe`，
  无窗口主程序派生的控制台子进程会让 Windows 弹出可见终端。现统一加 `CREATE_NO_WINDOW`。
- 剪贴板读取崩溃：64 位句柄被截断成 32 位导致随机 access violation，会静默终止同步循环。

## [v2.2.0] - 2026-10-29

### 变更
- 三个 exe（`RelayDesk.exe` / `flixd.exe` / `flix.exe`）合并为单文件 `RelayDesk.exe`，
  子命令 `serve` / `mcp` 分别对应后台服务与 AI 插件入口。
- 窗口连不上本机服务时自动拉起一次后台服务并在 3.5 秒后重连，不再停在离线页。

## [v2.1.0] - 2026-10-29

### 新增
- 双机 AI 对 AI 通道：`action=rdAgent` 信封复用 8891 与既有配对关系，不新开端口；
  未配对设备投递返回 403 且不落库。
- 三个 MCP 工具：`flix_agent_send` / `flix_agent_inbox`（可长轮询）/ `flix_agent_reply`。
- outbox：对端离线时消息留在本机，守护进程每 30 秒重投，调用立即返回 `queued`。

## [v2.0.0] - 2026-10-29

### 新增
- Windows 桌面端：群管理（改名/加人/移人/新建）、桌面端发起配对、图片与文件气泡内联渲染、
  应用自绘通知卡片、会话复制（`Ctrl+C` / `Ctrl+Shift+C`）。
- Android 平板端：页头与分段控件重排、卡片式列表与空状态、会话页视觉统一。
- 配对后自动补推群快照；修复测试污染真实配置导致收文件静默失败。

[v2.2.1]: https://github.com/XinwangTao/flix-cli/compare/v2.2.0...v2.2.1
[v2.2.0]: https://github.com/XinwangTao/flix-cli/compare/v2.1.0...v2.2.0
[v2.1.0]: https://github.com/XinwangTao/flix-cli/compare/v2.0.0...v2.1.0
[v2.0.0]: https://github.com/XinwangTao/flix-cli/releases/tag/v2.0.0
