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

## 设备在线判定与诊断

在线状态（RelayDesk 侧边栏绿点）= 设备有 IP 且 `seenAt` 在 **90s** 内。官方客户端只在启动时
announce，因此 flixd 每 60s 对库内设备做一次 `/ping_v2` 探活并刷新 `seenAt`；探活失败不改写
时间戳，设备自然显示离线，重启后不会再把历史设备画成绿点。

`flix netcheck [--json]` 是只读诊断：选中网卡、组播 join（临时端口，不抢占运行中的 8891）、
8891 占用、防火墙配置、库内设备端口可达性。对端设备通道不通时，把
`scripts/fix_firewall.ps1` 交给**对端**管理员，在其机器上以管理员 PowerShell 执行
（默认放行 `dist\flixd.exe`，`-Remove` 撤销）；flixd/RelayDesk 不会自动执行它，也不会改动远端。

## 功能矩阵

| 功能 | CLI | MCP 工具 | 官方互通 |
|---|---|---|---|
| 发现（UDP 224.0.0.168:8891，启动突发 11 包） | `flix discover [--announce]` | flix_discover | ✅ |
| 探测 | `flix probe <ip[:port]>` | flix_probe | ✅ 404 "Route not found" |
| 局域网诊断（网卡/组播/防火墙/端口可达） | `flix netcheck [--json]` | — | — |
| 手动添加（ping_v2 握手） | `flix ping <ip[:port]>` | — | ✅ 官方 UI 添加成功 |
| 配对（askPairDevice/confirmPairDevice） | `flix pair [--code]` | flix_pair | ✅ 双侧 pair_devices 落库 |
| 文本气泡（type 0） | `flix send-text` | flix_send_text | ✅ |
| 剪贴板投递（TransIntent clipboard） | `flix clipboard` | flix_send_clipboard | ✅ |
| 文件传输（bubble + POST /file，share_id=bubbleId，.flixdownload 原子写） | `flix send` | flix_send_files | ✅ 配对后官方自动收 |
| 剪贴板自动同步（官方 sendClipboard 语义） | flixd 内置（`--no-clip-sync` 关） | — | ✅ 官方实测到达 |
| 群组（本地群表 + bubble+groupId 逐发） | `flix group create/add/join/leave/list/send/history` | flix_group_* | ✅ 官方可收 |
| 配对二维码 | `flix qrcode [--decode]`（flix2:// payload） | — | flixcli 间 |
| 自动应答（轮询收件 → `--cmd` → 回发，带防自激护栏） | `flix agent --cmd "…"` | — | — |
| 远程执行（rdShell，exec-only，配对即可执行） | `flix shell <设备> -- <命令>` | flix_shell | flixcli 间 |
| Web 界面（PIN 登录 → 发送页 + **群聊实时视图**：历史+3s 轮询） | `flixd --web-port 18992`（`flix serve` 同） | — | 群视图为 flixcli 附加 |
| WebRTC 信令面（offer/answer/ice/hangup） | REST `/webrtc/signal`、`/api/webrtc/*` | — | flixcli 间 [compat] |
| Desktop API（self/devices/settings/events SSE/send-text） | `http://127.0.0.1:8898` | flix_status | 形状对齐 |
| Windows 桌面控制台 | `flix gui`（PySide6 Essentials） | — | 自有 GUI |
| Android ADB 设备/报告 | `flix adb devices|report` | flix_adb_devices / flix_adb_report | 本机 USB/无线 ADB |
| ADB 文件往返 | `flix adb push|pull` | flix_adb_push / flix_adb_pull | 本机授权设备 |

## AI 接入（MCP）

`flix mcp` 是 stdio JSON-RPC MCP server。客户端配置：

```json
{ "mcpServers": { "flix": { "command": "C:\\path\\to\\flix-cli\\.venv\\Scripts\\flix.exe",
                            "args": ["mcp"] } } }
```

工具（20）：原有设备/消息/群组工具，加上 `flix_adb_devices`、`flix_adb_report`、
`flix_adb_push`、`flix_adb_pull`。ADB 工具要求明确的 `serial`（多设备时不猜目标），
只执行文件传输和只读报告；shell 不通过 MCP 暴露。

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

## 回归测试

```
python tests/e2e.py         # 双节点 11 项：ping/pair/text/file/group/web/
                            # webrtc/qrcode/clipboard（需 18991/18995 空闲）
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

## 许可与反馈

本项目以 [MIT](LICENSE) 许可发布。版本变更见 [CHANGELOG.md](CHANGELOG.md)，
安全问题的报告方式见 [SECURITY.md](SECURITY.md)。

Issues / PR 欢迎；提交前请先在本机跑一遍 `pytest tests -q --ignore=tests/e2e.py`。
