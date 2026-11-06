# flix-cli

[![CI](https://github.com/XinwangTao/flix-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/XinwangTao/flix-cli/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Release](https://img.shields.io/github/v/release/XinwangTao/flix-cli?label=release)](https://github.com/XinwangTao/flix-cli/releases)

Flix 2.4.0+127（com.ifreedomer 江河智连）局域网互操作复刻：`flixd` 守护进程 + `flix` CLI +
`flix mcp`（AI 插件接口）。以 IDA MCP 静态逆向 + 线上 verbatim 取证为基准实现
（协议规格与证据标注见 `reverse/protocol.md`），与官方 Flix 客户端实测互通。

> **English** — An independent, interoperable reimplementation of the Flix LAN transport
> (discovery, pairing, text/file bubbles, groups, clipboard sync), shipped as a Python CLI and
> daemon, a stdio MCP server for AI clients, a PySide6 desktop console, and an Android client.
> Windows is the primary target; the CLI and daemon also run on Linux/macOS.

```bash
git clone https://github.com/XinwangTao/flix-cli.git
cd flix-cli
python -m venv .venv
.venv\Scripts\pip install -e .        # Windows / CLI + daemon
.venv\Scripts\pip install -e ".[gui]" # Windows 桌面 GUI
# source .venv/bin/activate && pip install -e .   # Linux/macOS

flixd                       # 常驻：设备通道 8891 + Desktop API 8898（默认）
flix gui                    # 打开桌面控制台（需 [gui] extra）
```

与官方客户端同机共存：`flixd --device-port 18991 --api-port 18998`。

发现组播默认走系统路由选中的网卡，并跳过回环/APIPA 与 **/32 虚拟网卡**（VPN 网关如 `vgate0`
常是 /32，会抢走组播路由导致 announce 发到无对端的虚拟链路）。需要指定时用
`--multicast-if 192.168.1.23`（`flix discover` / `flixd` / `flix serve` 均支持），或设
环境变量 `FLIX_MULTICAST_IF`；启动日志会打印实际使用的网卡。

announce 同时以**局域网广播兜底**（子网定向 + 255.255.255.255，端口不变）：部分企业安全代理
（Tencent iOA、Sangfor 等内核 callout）会在协议栈内吞掉全部出站多播（pktmon 实证），广播是
这类网络上唯一能出卡的 announce；flixcli 间发现不受影响，官方客户端是否接受广播未证实。
flixd 启动突发后还会每 30s 重发一次（2 包），让晚启动的节点也能发现本机。

同一台机器可能同时跑默认 daemon（8891）和 `RelayDesk.exe serve --device-port 18991`：两者
指纹相同、各自每 30s announce。旧逻辑「最后一次 announce 胜出」会让对端每 30s 在 8891 与
18991 之间改写 `port`，消息/剪贴板/文件确认随机落到先到的进程，日志和 `devicesChanged` 也
跟着刷。现在按指纹记录 90s 内出现过的 `(ip, port)`，同一 IP 优先选**最小端口**（即协议默认
8891）；只有已选端口整整 90s 不再 announce，才切到仍在广播的备选端口。端口变化本身不再算
设备变化。

## 设备在线判定与诊断

在线状态（RelayDesk 侧边栏绿点）= 设备有 IP 且 `seenAt` 在 **90s** 内。官方客户端只在启动时
announce，因此 flixd 每 60s 对库内设备做一次 `/ping_v2` 探活并刷新 `seenAt`；探活失败不改写
时间戳，设备自然显示离线，重启后不会再把历史设备画成绿点。

**announce 本身也算心跳**：收到与上次相同的 announce（突发副本、30s 重发、广播兜底）也会
刷新 `seenAt`，按 15s 节流，不放大成每包一次写库，也不触发 `devicesChanged`/日志。这样
对端防火墙挡住入站 8891（`/ping_v2` 永远连不上）但仍在每 30s 广播的设备不会被 90s TTL
误判离线——它能听见我们、也能主动发消息过来，界面上必须还是绿的。e2e 用一条指向无人
监听端口的注入 announce 验证“探活不可达仍在线”（板端测试结束即删除该行）。

`flix netcheck [--json]` 是只读诊断：选中网卡、组播 join（临时端口，不抢占运行中的 8891）、
8891 占用、防火墙配置、库内设备端口可达性。对端设备通道不通时，把
`scripts/fix_firewall.ps1` 交给**对端**管理员，在其机器上以管理员 PowerShell 执行
（默认放行 `dist\flixd.exe`，`-Remove` 撤销）；flixd/RelayDesk 不会自动执行它，也不会改动远端。

## 跨平台剪贴板

`flixd` 的自动同步在 Windows 上全程走 user32 的 `CF_UNICODETEXT`（读写都不再经过
`clip.exe`：子进程会按控制台代码页重编码 stdin，实测把中文变成 `????`，而发送端仍收到
success）；Linux 桌面在 Wayland 下用
`wl-clipboard`，X11 下用 `xclip`/`xsel`；macOS 用 `pbpaste`/`pbcopy`。**无 GUI 的 Linux 板子**
（QCM6490 在 systemd/tty 下运行，没有 X/Wayland）拿不到剪贴板，收到远端剪贴板后不再静默丢弃，
而是原子写入 `~/flixcli/clipboard.txt`（0600，UTF-8），journal 打印
`[clip-sync] no GUI clipboard; stored N chars in …`，本机脚本/agent 可直接 `cat` 取用。
该文件只写不读：否则守护进程重启后会把上次收到的文本当成"新剪贴板"重新广播。
`/healthz` 的 `clipboard` 字段给出实际落地方式（`win32` / `wl-clipboard` / `xclip` /
`xsel` / `pbcopy` / `file` / `none`），多端排障时先看这一项。

`flix clipboard <ip> -`、`flix send-text <ip> -`、`flix group send <group> -` 都从 stdin
读正文，且**按 UTF-8 解**（BOM 去掉、CRLF 折成 LF；真不是 UTF-8 时才退回本机代码页）。
Windows 上重定向的 stdin 默认按控制台代码页解码，管道里的 UTF-8 中文会变成孤立代理字符，
`json.dumps` 当场抛 `UnicodeEncodeError`，对端什么都收不到——这也是"剪贴板发了但板上是空的"
的常见原因之一，先看发送端有没有 traceback。长正文请走 `-`：Windows 命令行只有 32767 字符。

## 跨平台文件名

接收端按**本机**的规则清洗对端文件名，对端库里/气泡里的名字保持原样，
`fileReceived.savedAs` 报实际落盘名。板子（Linux）里 `linux:colon?.txt`、`a*b`、
`CON`、结尾带点的名字都合法，直接往 Windows 上写会出事——而且 `:` 比"拒绝"更糟：
NTFS 把它当**数据流**分隔符，占位文件建成了另一个名字（`~/Downloads/linux`），
正文全部收完才在 `os.replace` 处以 WinError 123 失败，发送端看到 400 `upload-failed`，
`Downloads` 里还多一个空文件。现在 Windows 上把 `<>:"/\|?*` 和控制字符换成 `_`、
去掉结尾的点/空格、给 `CON/NUL/COM1…` 这类设备名加 `_` 前缀；POSIX 只去掉 NUL，
所以板上仍能收到带 `:` 的原名。端到端回归见 `scripts/e2e_two_nodes.py` 的
"板→PC 文件（Windows 非法名）" 三项。

**反斜杠不是对端文件名的路径分隔符**：Linux 允许 `back\slash.txt` 是一个普通文件。
旧实现先按 `\` 和 `/` 一起切 basename，板上发这种名字时 PC 落成 `slash.txt`——前缀
悄悄消失，`a\x.txt` 与 `b\x.txt` 还会互相撞名变成 `x (1).txt`。现在只按 `/` 切路径，
Windows 再把 `\` 和其他非法字符一样换成 `_`（`back_slash.txt`），POSIX 原样保留。
`/file` 的 `file_name` 校验也改用同一套清洗结果比较，所以板↔板、板→PC 都不再误判
400 `bad file name`。回归：e2e "板→PC 文件名含反斜杠" 与
`tests/test_transfer_security.py::test_backslash_in_peer_name_is_not_a_path_separator`。

名字合法还不够——**长度**也得按接收端算。ext4 的 `NAME_MAX` 是 255 **字节**，
NTFS 是 255 个 UTF-16 单元（没开长路径时整条路径还得 <260 字符）。PC 上一个
`长文件名×30.txt`（364 字节）在 Windows 上完全合法，往板上发时接收端 `O_EXCL` 占位直接
`ENAMETOOLONG`，旧代码把它吞成 409 `destination exists`（其实一个字节都没写），发送端只看到
`upload-failed`。现在接收端先按 `os.pathconf(..., PC_NAME_MAX)`（Windows 按组件 255 +
长路径开关）**收窄**：保留扩展名、在 UTF-8/UTF-16 字符边界上截断主体、留出 8 字节给
`" (n)"` 同名后缀，`fileReceived.savedAs` 报收窄后的名字，气泡/库里仍是原名。占位失败也改报
真实原因（`cannot reserve destination: File name too long`），不再一律甩 409。
两端回归见 e2e 的 "PC→板 超长文件名送达 / 板端按 NAME_MAX 收窄且内容一致"。

## 功能矩阵

| 功能 | CLI | MCP 工具 | 官方互通 |
|---|---|---|---|
| 发现（UDP 224.0.0.168:8891，启动突发 11 包） | `flix discover [--announce]` | flix_discover | ✅ |
| 探测 | `flix probe <ip[:port]>` | flix_probe | ✅ 404 "Route not found" |
| 局域网诊断（网卡/组播/防火墙/端口可达） | `flix netcheck [--json]` | — | — |
| 手动添加（ping_v2 握手） | `flix ping <ip[:port]>` | — | ✅ 官方 UI 添加成功 |
| 配对（askPairDevice/confirmPairDevice） | `flix pair [--code]` | flix_pair | ✅ 双侧 pair_devices 落库 |
| 文本气泡（type 0） | `flix send-text` | flix_send_text | ✅ |
| 文本持久投递（离线排队 → 30s 重投 → TTL 7 天） | `flix outbox` | — | 线格式不变 |
| 剪贴板投递（TransIntent clipboard） | `flix clipboard` | flix_send_clipboard | ✅ |
| 文件传输（bubble + POST /file，share_id=bubbleId，.flixdownload 原子写） | `flix send` | flix_send_files | ✅ 配对后官方自动收 |
| 剪贴板自动同步（官方 sendClipboard 语义） | flixd 内置（`--no-clip-sync` 关） | — | ✅ 官方实测到达 |
| 群组（本地群表 + rdGroupSync 快照 + bubble+groupId 逐发） | `flix group create/add/join/leave/list/send/history` | flix_group_* | ✅ 官方可收 |
| 配对二维码 | `flix qrcode [--decode]`（flix2:// payload） | — | flixcli 间 |
| 自动应答（轮询收件 → `--cmd` → 回发，带防自激护栏） | `flix agent --cmd "…"` | — | — |
| 远程执行（rdShell，exec-only，配对即可执行） | `flix shell <设备> -- <命令>` | flix_shell | flixcli 间 |
| Web 界面（PIN 登录 → 发送页 + **群聊实时视图**：历史+3s 轮询） | `flixd --web-port 18992`（`flix serve` 同） | — | 群视图为 flixcli 附加 |
| WebRTC 信令面（offer/answer/ice/hangup） | REST `/webrtc/signal`、`/api/webrtc/*` | — | flixcli 间 [compat] |
| Desktop API（self/devices/settings/events SSE/send-text） | `http://127.0.0.1:8898` | flix_status | 形状对齐 |
| Windows 桌面控制台 | `flix gui`（PySide6 Essentials） | — | 自有 GUI |
| Android ADB 设备/报告 | `flix adb devices|report` | flix_adb_devices / flix_adb_report | 本机 USB/无线 ADB |
| ADB 文件往返 | `flix adb push|pull` | flix_adb_push / flix_adb_pull | 本机授权设备 |

## 跨端群组状态

群组不是"官方服务端下发"的对象：每个节点各存一份群表，消息仍是 `bubble + groupId`
逐成员发送。所以**建群/加人必须主动把快照推给成员**（`rdGroupSync`，`rev` 取毫秒时间戳，
严格新者胜）。桌面 GUI 一直这样推，CLI 的 `flix group create --member` / `flix group add`
原先只写本机库：PC 上建好群、消息也 `delivered`，板子上 `flix group list` 却是空的、
`flix group history <gid>` 报 `no-group`——气泡其实按 `groupId` 落库了，只是没有对应的群行，
等于"收到但看不见"（实测板端积压了多个这样的孤儿群）。现在两个 CLI 入口与 GUI 一致，
建群/加人后立即推送并逐个报告 `delivered/total/results`。

接收端还有兜底：只收到带 `groupId` 的气泡、却从没收到过快照时（旧版对端、或对方直接
`group send`），按**已配对发送方**补登记一个最小群组（本机 + 发送方，`rev=0`），让
`flix group list/history` 立刻能查到这条线程；之后任何真实快照（`rev`>0）都会覆盖占位名与
成员表。未配对发送方的气泡仍不建群（与 `/intent` 的信任边界一致）。板子上
`flix group history <gid> --json` 是排查"群里发了消息但对方说没看到"的第一站：先确认群行
在不在，再看 `bubble_entities.group_id`。e2e 的“CLI 建群把快照推给板子 / 板子按群查得到
跨端群消息 / 板子自动登记只收到气泡的陌生群组”三项覆盖整条链路。

## AI 接入（MCP）

`flix mcp` 是 stdio JSON-RPC MCP server。客户端配置：

```json
{ "mcpServers": { "flix": { "command": "C:\\path\\to\\flix-cli\\.venv\\Scripts\\flix.exe",
                            "args": ["mcp"] } } }
```

工具覆盖设备/消息/群组/ADB/AI 通道/远程执行，包括 `flix_adb_devices`、
`flix_adb_report`、`flix_adb_push`、`flix_adb_pull`、`flix_agent_*` 与 `flix_shell`。
ADB 工具要求明确的 `serial`（多设备时不猜目标），只执行文件传输和只读报告；
**ADB shell 仍不通过 MCP 暴露**，远程命令请走 `flix_shell`（exec-only，见「远程执行」）。

局域网多设备 AI 通讯：每台设备跑 `flixd` + `flix mcp`，AI 之间即可互发文本/剪贴板/
文件/群消息：

```
flix_status → flix_discover → flix_send_text <fp> "…"      # 点对点
flix_group_create "ops" → flix_group_add ops <fp2> …        # 多设备群
flix_group_send ops "广播指令"                               # AI 群发
flix_bubbles                                                # 读收件
```

## 远程会话（自研端）

首批远程能力只支持 flixcli↔flixcli Windows 局域网一对一。请求默认仅观看，音频和控制必须显式
请求并由接收端本地批准；控制不执行命令，只允许受限鼠标/滚轮/按键白名单。安装媒体依赖：

```bash
.venv\\Scripts\\pip install -e ".[media]"  # aiortc + av + mss + Pillow + numpy
flix call --json request <fingerprint> [--audio] [--control]
flix call --json list
flix call --json approve <call-id> [--control]
flix call --json connect <call-id>
flix call --json revoke <call-id>
```

媒体使用局域网 host ICE，不接公网中继；权限、直连或编解码失败会返回明确错误并降级，不宣称与官方
Flix WebRTC/远控互通。会话票据通过临时 X25519 密钥交换派生 HMAC 密钥，线上不传共享密钥。

## Android / ADB 联调

```bash
flix adb --json --serial cc75218b devices
flix adb --json --serial cc75218b report
flix adb --json --serial cc75218b push ./photo.png /sdcard/Download/photo.png
flix adb --json --serial cc75218b pull /sdcard/Download/photo.png F:\\cache\\photo.png
```

当前设备可能同时显示 USB 序列号和无线序列号；必须显式指定 `--serial`，避免传输目标歧义。
ADB shell 默认只返回 `approval_required`；只有本机操作者显式加 `--approve` 才会执行。
通过 Git Bash 调用时，适配器会还原被 MSYS 转换的 `/sdcard/...` 远端路径。

## 桌面控制台

`flix gui` 展示 daemon 统一状态：局域网设备、ADB 设备、最近消息和传输工作区。
GUI 通过 `127.0.0.1:8898` Desktop API 工作，CLI/MCP 与 GUI 读取同一状态。若缺少依赖，
执行 `pip install -e ".[gui]"`。

## 自动更新

RelayDesk 窗口启动后会自己检查 GitHub Release（设置里可关闭）：发现新版本就在后台下载、
校验 sha256，窗口顶部出现横幅；点「重启并安装」才替换 `RelayDesk.exe` 并按原参数重启，
起不来自动回滚。下载期间不阻塞聊天，MCP 进程不会被重启。

同一套引擎也可以用 CLI 手动执行：`flix update --check` 只查（有新版本退出码 10），
`flix update` 直接安装，`--dry-run` 停在替换前。源码检出可
`python scripts\autoupdate.py --check`。状态与日志在 `%APPDATA%\flixcli`，细节见
[docs/autoupdate.md](docs/autoupdate.md)。

## 自动应答（flix agent）

`flix mcp` 是被动的：AI 会话被唤醒才会收发。要无人值守地互相应答，用 `flix agent`——
轮询收件、把新消息交给任意命令、把命令的 stdout 回给对方：

```bash
flix agent --cmd "F:\my-ai\reply.bat" --prefix "[A-AI]"
```

- **输入**：JSON（**UTF-8**）走 stdin（`id / from / groupId / text / time / hop`），另给
  `FLIX_SELF_FP`、`FLIX_SELF_ALIAS` 两个环境变量；脚本自身请 `reconfigure(encoding="utf-8")`，
  未适配的脚本输出会按 utf-8→gbk 容错解码；
- **输出**：stdout 原样作为回复（自动加 `--prefix` 和 `[[hop=N]]` 标记）；空输出 = 不回，
  非零退出 = 记失败不重发；
- **路由**：带 `groupId` 的回群，否则回给发送方（需要该设备已 `pair` 过）；
- **护栏**（防两个 agent 无限对喷）：只回已配对设备、跳过带本机前缀的消息、
  同一对端默认 ≥30 s 一次（`--min-gap`）、链式默认 ≥2 跳即停（`--max-hop`）；
- 状态在 `%APPDATA%\flixcli\agent_state.json`，日志在 `agent.log`；`--once` 单轮、
  `--dry-run` 只记录不发送，便于调试。

流程真源：`docs/archify/flix_agent_workflow.html`（旁路 `.json`）。

## 可靠投递（outbox）

文本气泡先落库再发送，只有对端回 `200 received bubble` 才算送达；对端暂时不在时消息进队列，
守护进程每 30 秒重投，超过 7 天标记过期。

```bash
flix send-text <设备> "…"      # 返回 state=delivered|queued
flix outbox                    # 查看队列：queued / delivered / failed / expired
flix outbox --retry <id>       # 手动重投
flix outbox --drop <id>        # 丢弃
```

- **4xx 不重试**：未配对、参数错误属于永久失败，直接标 `failed`。
- **只有幂等动作排队**：`flix shell`、`flix upgrade` 这类执行类动作重复跑有副作用，仍然只发一次。
- **按 IP 排队的消息也会落地**：`flix send-text 192.168.1.9 …` 在对端还没 announce 过时，
  队列行只能把地址当对端标识。重投时按 `resolve_target` 把 `ip[:port]` 映射回库里该地址
  最新的设备指纹，并回写队列行与气泡的 `to`（协议字段应是指纹）；否则重投永远用
  `device_ip(IP)` 精确匹配指纹查不到地址，消息每 30 秒被静默跳过，直到 7 天过期。
  e2e 用一条直接注入的 IP-peer 队列行验证“对端出现后自动送达并回写指纹”。
- 文件与剪贴板暂不排队（二期沿用同一张表）。

流程真源：`docs/archify/flix_outbox_workflow.html`（旁路 `.json`）。

## 远程执行（flix shell）

两台**已配对**设备之间跑一条命令并取回结果，相当于局域网里的 `ssh <host> <cmd>` 最小面。
一期只做 exec：不分配 PTY，所以 `vim`/`top` 这类全屏程序不支持。

```bash
flix shell 4c4ca286 -- "ipconfig /all"        # 指纹前缀即可
flix shell 192.168.237.200:18991 -- "whoami"
flix shell <设备> --timeout 120 -- "ping -n 5 127.0.0.1"
```

- **返回**：`stdout` / `stderr` / `exit` / `truncated` / `durationMs`；CLI 的退出码就是远端命令的退出码。
- **上限**：输出超过 256 KB 截断并置 `truncated`；超时默认 30 s、最大 300 s，到点杀整个进程树。
- **授权**：配对即授权（两端都查 `pair_devices`），未配对一律 403，不执行、不落库。
- **审计**：目标端 `shell_audit` 表记录发起方、命令摘要、退出码、耗时；`stdout` 不回写数据库。
- **MCP**：`flix_shell{to, command, timeout?}` 与 CLI 共用同一实现。

流程真源：`docs/archify/flix_shell_workflow.html`（旁路 `.json`）。

### 交互式终端（`--pty`，二期）

```bash
flix shell --pty 4c4ca286                 # 默认 cmd
flix shell --pty --shell powershell 4c4ca286
```

- 目标端用 **ConPTY**（`pip install -e ".[pty]"`）分配伪终端，`vim`/`top` 这类全屏程序可用；
- 客户端把本机控制台切到 raw 模式并透传 ANSI，**Ctrl+]** 退出会话；
- 信任门与 exec 相同（配对 + 来源 IP 一致）；缺 `pywinpty` 时明确报 `pty-unavailable`，请退回 exec；
- 空闲 30 分钟自动断开，同一时间只允许 1 个会话；会话结束写一条审计（时长/退出码）。

流程真源：`docs/archify/flix_shell_pty_workflow.html`（旁路 `.json`）。

## 回归测试

```
python tests/e2e.py         # 双节点 11 项：ping/pair/text/file/group/web/
                            # webrtc/qrcode/clipboard（需 18991/18995 空闲）
python scripts/interop_check.py --my-fp <fp> --official-fp <fp> --self-test
                            # 与原生 Flix 客户端的线格式互通（官方客户端在跑时）
```

## 退出码约定

`flix shell` 例外：成功连上并执行后，退出码就是远端命令的退出码（与 ssh 一致），
网络/拒绝/本地问题仍用下表。

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 2 | 用法错误 |
| 3 | 网络失败（无设备/超时/不可达） |
| 4 | 对端拒绝（传输被拒等） |
| 5 | 本机环境/协议未就绪（端口占用、防火墙） |
| 10 | `flix update --check`：发现新版本 |
| 130 | Ctrl+C |

## 目录

- `src/flixcli/` 实现代码（config/model/discovery/channel/server/webapp/webrtc/
  files/groupchat/routing/daemon_runtime/desktop_api/mcp_server/qrcode/cli）
- `tests/e2e.py` 双节点回归
- `docs/archify/` 已确认流程图（真源）、`docs/product/` 项目 brief
- `reverse/` 逆向真源：`protocol.md`（协议规格+证据标注）、池解析 dump、
  `pool_rebase.py`/`scan_full.py` 等管线工具、`wire_capture.log` 抓包

## 已知边界

- 官方「创建群组」为 Flix MAX 付费功能；群 resolve 的**创建方应答**格式未解
  （官方对构造应答静默丢弃），flixcli 间群聊用本地群表 + groupId 逐发（官方可收）。
- flixcli 自有 LAN 媒体面已实现：X25519/HMAC 会话认证、host ICE、主屏视频快照、分级批准和有限控制；
  已通过单机 offer/answer、双隔离 HTTP 节点和主屏单帧验收。真实两台 Windows 的持续视频帧、控制效果、
  音频样本与断线清理仍待对端升级同一版本；官方 Flix WebRTC/远控互通不承诺。
- `/webrtc/signal` 是旧实验性信令兼容面，不能把其 HTTP 200 当作媒体完成；新媒体走
  `/webrtc/v1/session`、`/webrtc/v1/signal`，要求配对、来源 IP、nonce/seq 和 HMAC。
- Windows GUI 已扩展远程请求、分级批准、快照预览和有限控制；音频采集/播放仍未接入。
- 二维码 payload 为 flixcli 自有 `flix2://` 格式（官方编解码待解）。
- 合规约束：不复制品牌资源、不绕过付费、不上传敏感数据。

## Windows exe 打包

```powershell
powershell -File scripts\build_exe.ps1     # 产物：dist\flix.exe、dist\flixd.exe（onefile）
```

`flixd.exe --device-port 18991 --api-port 18998 --web-port 18992` 即完整守护进程
（发现/配对/群/文件/剪贴板同步 + 网页界面）；`flix.exe` 含全部 CLI 子命令、
`mcp`（AI 插件）与 `agent`（AI 自动应答）。状态仍在 `%APPDATA%\flixcli`。

## macOS .app

GitHub Actions 在 `macos-14`（Apple Silicon）和 `macos-15-intel`（Intel）上构建
`RelayDesk-macos-<arch>.zip`，workflow 为 `.github/workflows/build-macos.yml`。
本地构建：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[gui]" pyinstaller
pyinstaller RelayDesk-macos.spec --noconfirm --clean
open dist/RelayDesk.app
```

应用未签名、未公证。首次打开若提示「无法验证开发者」：右键 → 打开，或执行
`xattr -dr com.apple.quarantine /Applications/RelayDesk.app`。
状态目录为 `~/Library/Application Support/flixcli`。

## 许可与反馈

本项目以 [MIT](LICENSE) 许可发布。版本变更见 [CHANGELOG.md](CHANGELOG.md)，
安全问题的报告方式见 [SECURITY.md](SECURITY.md)。

Issues / PR 欢迎；提交前请先在本机跑一遍 `pytest tests -q --ignore=tests/e2e.py`。

## 部署到 ARM 板子（移远 QCM6490 / Qualcomm Linux）

板子是 aarch64 + Python 3.10，项目要求 3.11+，所以用 uv 自带解释器，不动系统 Python。

```bash
scripts/deploy_board.sh root@quecpi-alpha          # 打包 → 传输 → uv 装 3.12 → systemd 起服务
```

- 安装位置 `/opt/flixcli`，服务 `flixd.service`（`Restart=always`、`MemoryMax=256M`、别名取主机名）。
- 网页版：`http://<板子IP>:8892`（PIN 在 `journalctl -u flixd` 里）。
- 设备通道 8891 对外，桌面 API 8898 仍只绑 127.0.0.1。
- 换网自愈：announce 循环每 30s 核对出口网卡，网线拔插或热点回退后会重新加入组播，
  否则 TCP 通、对端却再也发现不了这台机器。
- 板载 QCA1023 Wi-Fi 的驱动不声明 802.11w(PMF)，只能连不强制 PMF 的 AP；
  强制 PMF 的企业 AP 会握手成功后几秒内以 reason=148 踢掉。

细节（healthz 自检、无头配对、Wi-Fi 限制）见 `docs/deploy-board.md`。
