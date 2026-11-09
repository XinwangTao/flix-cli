# 变更记录

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；版本号与 GitHub Release 标签一致。
可执行产物见 [Releases](https://github.com/XinwangTao/flix-cli/releases)。

## [v2.4.0] - 2026-11-09

### 新增
- Windows 11 原生 Acrylic 毛玻璃：由 DWM 实时渲染窗口背后的内容，窗口移动/换背景都正确；
  DWM 不可用时自动回落到原有的截图材质。
- Windows 发布拆成两个包：默认 `win64.zip`（lite，不含媒体栈）与 `win64-full.zip`
  （含 aiortc/av/numpy/PIL，保留屏幕共享）。自动更新默认只取 lite，已安装 full 的机器保持 full 通道。

### 变更
- 打包裁剪：不再收集未使用的 Qt 模块（Qml/Quick/Pdf/虚拟键盘/OpenGL）、软件 OpenGL 回退、
  多余插件与 140+ 语言翻译；默认不打包 media 可选依赖。lite 包从约 109 MB 降到约 42 MB。
- Windows lite/full 包均包含 PTY 支持（`.[gui,pty]`），`shell --pty` 在冻结包中可用。
- `flix ui`（Tk 窗口）退役，调用时返回明确提示并指向 `flix gui`。

### 修复
- 单文件包体积与启动时的临时解压开销（Qt 与 media 依赖不再整包塞入）。
- 毛玻璃材质在窗口移动后裁错区域、显示冻结桌面快照的问题（改用系统材质后不再有自绘层）。

## [v2.3.0] - 2026-11-06

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

### 新增
- 窗口材质：可选的毛玻璃效果，取桌面快照后模糊，只作用于标题栏、侧栏、状态栏等功能层，
  内容层保持不透明，保证正文对比度；设置页可随时开关，立即生效并在下次启动沿用。
- 动效偏好：跟随系统「显示动画」设置，关闭后分区切换不再淡入，只保留直接切换。
- 高对比度：系统开启高对比度时自动停用毛玻璃并把开关置灰，用户的偏好保留，关闭高对比度后恢复。
- 窄窗口适配：窗口宽度小于 1240 px 时侧栏由 320 px 收窄到 280 px，实体与会话名按实际
  宽度省略，不再依赖构建时的宽度估算。

### 新增（内置自动更新与键盘导航）
- `flix update`：跟 GitHub Release 自动更新 `RelayDesk.exe`。sha256 校验 → 停进程 →
  换文件（旧版另存 `prev-<stamp>`）→ 按原参数重启 → 健康检查，失败自动回滚。
  `--check` 有新版本时退出码 10，`--dry-run` 停在替换前。
- `scripts/autoupdate.py`：无 `flix` 命令的源码检出可直接跑同一套更新引擎（排障用）。
- 内置自动更新：窗口显示 3 秒后检查一次 Release（设置可关），发现新版后台下载并校验
  sha256，顶部常驻横幅提示；点「重启并安装」才替换并重启。不注册计划任务，不阻塞聊天。
- 键盘导航：`Ctrl+1`…`Ctrl+6` 在设备/搜索/群组/传输/ADB/设置之间跳转，`Esc` 从侧页或远程视图
  回到对话（按钮 tooltip 同步标注）。
- 设置页可检查并安装更新：“检查更新 / 立即更新并重启”直接调用内置的 `flix update`
  （sha256 校验 + 健康检查 + 失败回滚），窗口内无需终端；同时显示当前客户端版本。

### 修复（自动更新与界面导航）
- 自动更新会把自己的 `RelayDesk.exe update` 进程列入待杀名单，GUI 点「立即更新」会在换文件前
  把自己终止；现按 PID 排除自身。同时不再重启 `mcp` stdio 子进程（会让编辑器的 MCP 服务器
  变成死连接），只重启 GUI / `serve` / `agent`。
- 任何会话被打开后，2.5s 一次的状态轮询都会把窗口强制切回聊天页，设置/传输/ADB 实际看不清；
  现轮询只刷新消息流，导航权只属于点击与快捷键（`tests/test_gui_navigation.py`）。
- 侧页（传输/设置）左栏原本会清空成一整块空白；现保留会话列表，点一下就能回到对话。
- 侧页顶部仍显示上一段会话的标题，发送/搜索工具条也留在那里可点（会悄悄发到看不见的会话）；
  现侧页显示自己的标题并隐藏这些动作，返回对话时恢复。
- `flix update --dry-run` 原先在下载前就返回（只打一行 planned），与它自己的说明「下载并校验后再停」
  不一致；现真正走完 下载 → sha256 → 解包 → 布局校验 才停下，坏的发行包会在切换前就报错。
- `RelayDesk.spec` 打包配置被删掉了 winpty 收集与 `flixcli.cli/mcp_server/daemon/ptyshell`
  hiddenimports，现恢复并追加 `flixcli.autoupdate`（否则打包后 PTY 壳与子命令可能缺失）。
- 会话内搜索的词会带到下一段会话：换会话后消息被全部过滤，看起来像会话是空的；
  现切换会话即清空搜索（同一个会话内的轮询不会动它）。
- `__version__` 一直停在 `0.1.0`，导致 `flix --version`、`flix self`、MCP `serverInfo` 都报错版本；
  现与 `pyproject.toml` 对齐（`tests/test_version.py` 防止再漂移）。

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

[v2.3.0]: https://github.com/XinwangTao/flix-cli/compare/v2.2.1...v2.3.0
[v2.2.1]: https://github.com/XinwangTao/flix-cli/compare/v2.2.0...v2.2.1
[v2.2.0]: https://github.com/XinwangTao/flix-cli/compare/v2.1.0...v2.2.0
[v2.1.0]: https://github.com/XinwangTao/flix-cli/compare/v2.0.0...v2.1.0
[v2.0.0]: https://github.com/XinwangTao/flix-cli/releases/tag/v2.0.0
