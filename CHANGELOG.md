# 变更记录

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；版本号与 GitHub Release 标签一致。
可执行产物见 [Releases](https://github.com/XinwangTao/flix-cli/releases)。

## [未发布]

### 新增
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

### 修复
- 以 IP 为 peer 排队的文本永远不重投：`flix send-text 192.168.137.162 …` 在板子还没
  announce 过时，队列行的 `peer` 就是地址；`outbox.flush` 用 `device_ip(peer)` 只按指纹
  精确匹配，IP 键每 30 秒都查不到地址、被静默 skip，消息一直 `queued` 到 7 天 TTL 过期
  （对端其实早已恢复广播）。现在 flush 走 `resolve_target`，把 `ip[:port]` 映射回该地址
  最新的设备指纹（同 IP 多行取 `seenAt` 最新），并回写队列行与气泡 `to`，接收端拿到的
  收件人是指纹而不是 IP。实测：PC 上一条 peer=板子 IP 的 queued 行，在板子恢复 announce
  且 PC 换上新代码后由下一轮 flush 自动送达，`outbox` 显示 `delivered … 364220a6`。
  单测见 `tests/test_outbox.py`，e2e 新增“以 IP 为 peer 的排队消息在对端出现后送达”。
- 同机多 flixd 实例（默认 8891 + RelayDesk 的 18991）共用同一指纹时，双方的 30s announce
  会把对端库里的 `port` 每半分钟改写一次：板子的日志与 `devicesChanged` 反复刷新，消息、
  剪贴板、outbox 和文件确认在两个端口之间随机落点，停止其中一个实例后要到下一次 announce
  才能恢复。现在按指纹在 90s 窗口内记录所有 `(ip, port)`，同一 IP 优先选最小端口（协议默认
  8891），只有已选端口沉默满 90s 才切换；端口变化按心跳处理，不再触发设备更新。
  `tests/test_presence_announce.py` 覆盖稳定选择与 TTL 切换。
- 对端文件名只有一个字符的前缀时会被当成 Windows 盘符丢掉：板子发来
  `a:notes.txt`，接收端 `ntpath.basename` 认为 `a:` 是盘符，落盘名变成 `notes.txt`
  （多字符前缀如 `linux:colon?.txt` 反而正常，容易误判成“已修好”）。现在按最后一个
  `/` 手工切分再消毒，`a:notes.txt` → `a_notes.txt`。单测补在
  `tests/test_transfer_security.py`，e2e 新增“板→PC 单字符前缀文件送达 / 前缀未被当作盘符吞掉”。
- 按 IP 寻址时把地址当成了对端指纹：`flix shell 192.168.137.162` 明明两端已配对却回
  `not-paired`，`send-text`/`send`/剪贴板气泡的 `to`（deviceId）也写成 IP，接收端与官方
  客户端拿不到应有的指纹。`resolve_target` 现在用库内设备表把 `ip`/`ip:port` 映射回指纹
  （同 IP 多行按 `seenAt` 取最新，显式端口优先），未知地址仍按原样直连。e2e 新增
  “PC→板 远程执行（按 IP 寻址，需已配对）”，单测见 `tests/test_routing.py`。
- 仍在广播的设备被 90s TTL 误判离线：`seenAt` 原先只由 `/ping_v2` 探活刷新，收到与上次
  相同的 announce（突发副本、30s 重发、广播兜底）直接 return，不落库。对端防火墙挡住
  入站 8891 时探活永远失败，而设备仍在每 30s 广播——它听得见我们、也能主动发消息过来，
  RelayDesk 却把它画成离线。现在相同 announce 也刷新 `seenAt`（`Store.touch_device`，
  15s 节流，避免多播+两条广播的突发放大成每包一次写库），`devicesChanged` 与日志仍只在
  设备新增/模态变化时触发。e2e 新增“重复 announce 续期在线状态（探活不可达仍在线）”，
  单测见 `tests/test_presence_announce.py`。
- 长文件名跨端整次传输作废：PC 上的 `长文件名×30.txt`（364 字节）合法，板子 ext4 的
  `NAME_MAX` 只有 255 字节，接收端 `O_EXCL` 占位抛 `ENAMETOOLONG` 后被吞成
  409 `destination exists`（正文一个字节都没落盘），发送端只看到 `upload-failed`。
  现接收端按本机 `PC_NAME_MAX`（Windows 为组件 255，且未开长路径时整条路径 <260）收窄：
  保留扩展名、在字符边界截断主体、留 8 字节给 `" (n)"` 同名后缀，`fileReceived.savedAs`
  报实际落盘名；占位失败改报真实原因（`cannot reserve destination: …`）而不是一律 409。
  e2e 新增 “PC→板 超长文件名送达 / 板端按 NAME_MAX 收窄且内容一致” 两项
  （`tests/test_transfer_security.py::FileNameLengthTests`）。
- 板子（Linux）发来 Windows 非法文件名时整次传输作废：`linux:colon?.txt` 里的 `:` 被 NTFS
  当成数据流分隔符，占位文件建成了 `~/Downloads/linux`，正文收完后 `os.replace` 报
  WinError 123、接收端回 400 `upload-failed`，发送方以为文件没发出去，磁盘上还留一个空文件。
  现接收端按本机规则清洗文件名（`<>:"/\|?*`、控制字符、结尾点/空格、`CON/NUL/COM1…` 设备名），
  气泡与库里的原名不变，`fileReceived.savedAs` 报实际落盘名；POSIX 端不受影响
  （`tests/test_transfer_security.py`，e2e 新增板→PC 非法名三项）。
- `scripts/e2e_two_nodes.py` 在简体中文 Windows 控制台上跑到剪贴板项就 traceback
  （`UnicodeEncodeError: 'gbk' codec can't encode '✓'`），后面 4 项根本没执行却仍以非 0 退出，
  分不清"没测"和"测挂了"；现脚本统一 UTF-8 输出。同时补了板→PC 文件（含落盘内容与
  NTFS 空壳检查），端到端从 10 项扩到 14 项。
- 无头 Linux（QCM6490 板子）收到剪贴板时静默丢弃：`set_clipboard_text()` 在非 Windows 上直接
  返回，发送方却收到 `{0: success}`。现在 Linux 桌面用 `wl-clipboard`/`xclip`/`xsel`，
  macOS 用 `pbcopy`，无 GUI 时原子写入 `~/flixcli/clipboard.txt`（0600）并记日志；
  `/healthz` 新增 `clipboard` 字段报告实际落地方式（`tests/test_clipboard.py`）。
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

[v2.2.1]: https://github.com/XinwangTao/flix-cli/compare/v2.2.0...v2.2.1
[v2.2.0]: https://github.com/XinwangTao/flix-cli/compare/v2.1.0...v2.2.0
[v2.1.0]: https://github.com/XinwangTao/flix-cli/compare/v2.0.0...v2.1.0
[v2.0.0]: https://github.com/XinwangTao/flix-cli/releases/tag/v2.0.0
