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

## 自动连接（发现即配对）

设置页「自动连接」默认开启：同网段发现的未配对设备会自动发起配对，对端核验身份后直接
写库，两端都不需要点确认；GUI 设备设置里可单独关掉某台设备（prefs `autoConnect.devices`）。
自动配对只在"已在该 IP 发现过 + `ping_v2` 在该地址答出的指纹一致"时成立，伪造指纹不会被
信任；核验在后台完成，ask 立即回 `301`，不占对端 HTTP 预算。Android 侧开关是
`auto_connect`。模型共享（网关）上次开着的话，应用重启后会自己拉起。

## 设备在线判定与诊断

在线状态（RelayDesk 侧边栏绿点）= 设备有 IP 且 `seenAt` 在 **90s** 内。官方客户端只在启动时
announce，因此 flixd 每 60s 对库内设备做一次 `/ping_v2` 探活并刷新 `seenAt`；探活失败不改写
时间戳，设备自然显示离线，重启后不会再把历史设备画成绿点。

`flix netcheck [--json]` 是只读诊断：选中网卡、组播 join（临时端口，不抢占运行中的 8891）、
8891 占用、防火墙配置、库内设备端口可达性。

虚拟适配器按 `GetAdaptersAddresses` 的 FriendlyName/Description 整类排除（VMware、
VirtualBox、Hyper-V/vEthernet、Wi-Fi Direct、Bluetooth PAN、Wintun/Sangfor VPN、Clash
一类隧道），`netcheck` 会把它们列为 `skipped` 并带名字：这些网卡上没有任何对端，
而 VMware 的 VMnet1（192.168.73.1/24，无网关）在 Wi-Fi 闪断时会抢走组播路由，announce
打到那里等于本机从别人的设备列表里消失。选中的网卡还会被钉住，只有它真的消失
（拔网线/断网）才重新挑，不再每次轮询重猜。要手工钉死：`--multicast-if` /
`FLIX_MULTICAST_IF`，或写 `shared_preferences.json` 的 `multicast_if`（GUI 启动的
daemon 没有命令行参数，靠这一项）。对端设备通道不通时，把
`scripts/fix_firewall.ps1` 交给**对端**管理员，在其机器上以管理员 PowerShell 执行
（默认放行 `dist\flixd.exe`，`-Remove` 撤销）；flixd/RelayDesk 不会自动执行它，也不会改动远端。

## 功能矩阵

| 功能 | CLI | MCP 工具 | 官方互通 |
|---|---|---|---|
| 发现（UDP 224.0.0.168:8891，启动突发 11 包） | `flix discover [--announce]` | flix_discover | ✅ |
| 探测 | `flix probe <ip[:port]>` | flix_probe | ✅ 404 "Route not found" |
| 局域网诊断（网卡/组播/防火墙/端口可达） | `flix netcheck [--json]` | — | — |
| 手动添加（ping_v2 握手） | `flix ping <ip[:port]>` | — | ✅ 官方 UI 添加成功 |
| 配对（askPairDevice/confirmPairDevice，`--code` 必填：对端 4 位配对码） | `flix pair <ip[:port]> --code <码>` | flix_pair | ✅ 双侧 pair_devices 落库 |
| 文本气泡（type 0） | `flix send-text` | flix_send_text | ✅ |
| 文本持久投递（离线排队 → 30s 重投 → TTL 7 天） | `flix outbox` | — | 线格式不变 |
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
| Android ADB 设备/报告 | `flix adb devices` / `flix adb report` | flix_adb_devices / flix_adb_report | 本机 USB/无线 ADB |
| ADB 文件往返 | `flix adb push` / `flix adb pull` | flix_adb_push / flix_adb_pull | 本机授权设备 |

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

## 手机共享模型（局域网网关）

把本机 cc-switch 的模型共享给同网段的手机：`flix models start` 起一个
Anthropic Messages 端点（默认 `18993`），请求转发到本机 `127.0.0.1:15721`，
模型映射、账号分流和计费仍由 cc-switch 负责，RelayDesk 只保存一个访问令牌。
同一端口同时提供 **OpenAI 兼容**的 `POST /v1/chat/completions`（含 SSE 流式），
上游仍是同一条 Anthropic 路由，只多一层协议翻译。

- 手机端：Anthropic 客户端 Base URL 填 `http://<本机IP>:18993`；只支持 OpenAI 的客户端
  （大多数）协议选 OpenAI、Base URL 填同一地址，客户端会自己拼 `/v1/chat/completions`。
  API Key 填令牌。
  不确定填哪个地址时，用手机浏览器打开 `http://<本机IP>:18993/?token=<令牌>`：
  页面会按手机**实际访问用的地址**显示 Base URL/令牌、实时模型列表，并带一个
  「发送测试」按钮（可选 Anthropic / OpenAI 协议、可选流式，真跑一次
  手机→网关→cc-switch→模型）。
- OpenAI 兼容细节：`messages`（system/developer → 顶层 system；文本与 `image_url`
  两种 part；data URL 与 http 图片都转成 Anthropic image block）；`max_tokens` /
  `max_completion_tokens`、`temperature`、`top_p`、`stop`、`stream`、
  `stream_options.include_usage`；响应按 OpenAI 形状返回，推理模型的 thinking 放在
  `reasoning_content`，正文没产出时退化为 `content`（避免手机端空白）。
- **工具调用**：OpenAI `tools`/`tool_choice` 翻译成 Anthropic `tools`/`tool_choice`
  （`auto`→auto、`required`→any、指定函数→tool、`none`→不下发工具）；
  assistant 的 `tool_calls` 转 `tool_use`，`role=tool`（含 `tool_call_id`）合并成一条
  user 消息里的 `tool_result`（支持连续多条工具结果）。响应里的 `tool_use` 转回
  `tool_calls`、`finish_reason=tool_calls`；流式时按 OpenAI 的
  `delta.tool_calls[].function.arguments` 分片下发。`function_call`（旧字段）不处理。
- 模型名：`GET /v1/models` 是 cc-switch 当前 Claude 侧的实时目录（完整 id 形如
  `ccs-claude-<路由>--<模型>`），同时带 Anthropic（`type`/`display_name`）与
  OpenAI（`object`/`owned_by`/`created`）字段，两种客户端都能读。
  网关接受完整 id、**短名**（`glm-5.3-flash`）和旧版 `claude-*` 别名
  （`claude-sonnet-4-5`→glm-5.3、`claude-opus-4-1`→glm-5.3-flash、
  `claude-haiku-4-5`→kimi-k2.7-code、`claude-fable-5`→deepseek-v4.1-flash），
  短名会解析到实际可用的那条路由。目录里没有的名字会**直接 400 并列出可用模型**
  （避免 cc-switch 静默兜底成别的模型）；目录暂时拉不到时保持透传。
- 列表会自动收敛：某个 LAN 中继前缀不通时，它名下的模型默认**不出现在
  /v1/models**、手机下拉和 `flix models list` 里（hide_dead_relays，默认开），
  网关每 90 秒复探一次，恢复后模型立刻回来。置为 false 可恢复「显示并标注
  当前不通」的诊断视图。
- 路由回退：同一模型常有多条路由（本机直连 / 经另一台机器的中继）。某条路由 5xx 时
  自动换下一条再回给手机；失败路由记住 10 分钟，期间短名优先走好的那条，
  `/v1/models` 的 `display_name` 标注「（当前不通）」。成功响应的
  `x-relaydesk-route` 头写明最终用的是哪条路由。
  排序规则是**本机直连优先**（已知好/未知都排在中继前），中继只作补充；
  若某个中继前缀（如 `ccs-claude-cn222--`）失败过，同前缀的兄弟路由直接视为不可用，
  下次请求立即 503，不再让手机等满超时；中继成功一次即恢复。
  没验证过的中继路由会先做一次 **1-token 探测**（8s 上限、结果缓存 10 分钟）再决定
  是否转发，实测坏中继从 180s 降到 ~18s、再次请求 6ms。
- 超时预算：`upstream_timeout_s`（默认 180s，范围 10–900）是一次非流式请求的**总**预算，
  多条路由共用，不会每条各等一次；流式请求只按读取空闲时间计。
- **级联中继（LAN cascade）**：如果本机的上游是**另一台 RelayDesk 网关**（典型场景：
  这台机器没登录 VPN / 没有 cc-switch，而局域网另一台有），用
  `--upstream http://<对端>:18993 --upstream-token <对端令牌> --allow-remote-upstream`
  起网关即可；`upstream_token` 会作为 `x-api-key` 发给上游，客户端自己的令牌不会
  下发。目录拉取与中继探测也带这个令牌，因此第二跳不会 401。
  一键配置见 `scripts/setup_lan_model_source.ps1`（`-DryRun` 只看不改）。
- **开机自启 + 看门狗**：`scripts/peer_install_autostart.ps1 -Action Install` 会写登录
  自启 VBS 并建一个每 5 分钟检查的计划任务，serve 掉了自动拉起；
  `-Action Check` 只报告，`-Action Remove` 撤销。
- 令牌：首次开启自动生成；`flix models token --reset` 或设置页「重置令牌」轮换。
  同一来源 60 秒内连续 10 次错令牌会被临时锁 30 秒（429 + `Retry-After`），
  一个在猜令牌的邻居不会影响正常手机。
- 只开放 `POST /v1/messages`、`POST /v1/chat/completions` 与 `GET /v1/models`。
  CORS 预检与 `Access-Control-Allow-*` 已放行，手机浏览器/WebView 可直接调用。
- **并发不设上限（默认）**：网关照单全收，不再回 429；上游 cc-switch 自己排队。需要给
  单个令牌限流时显式设 `--max-concurrent 1..32`（`flix models start --max-concurrent N`）。
- **请求体**：上限 `max_body_mb`（默认 32 MiB，可配 1–128）；读取按块+空闲超时
  （20s 没数据才算停滞），慢速链路传大图不会被总超时误杀；超过上限立即回 413
  （带 Content-Length 的直接拒，不先缓冲）。
- GUI：设置页「模型共享」开关 + 扫码/复制配置（复制内容含实时模型短名）；
  `serve --model-port 18993` 供无界面部署。
- 状态与诊断：`flix models status`（监听、地址、令牌、上游链路、模型数、最近请求的
  耗时/状态/路由），`flix models list`（实时模型短名与完整 id）。
  地址列表会带网卡名（如 `http://192.168.137.1:18993（本地连接* 2）`），
  手机该填哪个一眼就能挑出来；自检页的「其它网卡地址」同样带名字。
  `flix models doctor` 一条命令体检整条链路（配置/监听/上游/路由/补全/手机地址），
  末尾直接给出手机端可照抄的 Base URL / API Key / 模型；本机健康时退出码 0，
  对端中继不通只作提示（不算本机故障）。
  `flix models doctor --watch 600` 持续观察并给漂移结论
  （模型数变化、新增失败、active 泄漏、补全变慢）。
  `python tests/acceptance_lan_models.py` 一条命令跑完 14 项功能验收
  （双协议、流式、工具调用、鉴权、并发、断开清理、自检页），发布前后各跑一次。

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
- **失败可诊断**：目标端起不来进程或执行器抛异常时，不再回裸 aiohttp 500，而是
  `{ok:false, error:"shell-failed", detail:"<异常类型>: <信息>"}`，同时 daemon 的 stderr
  记完整 traceback；`run_command` 自身起进程失败返回 `exit=127` + `stderr` 原因。
- **审计**：目标端 `shell_audit` 表记录发起方、命令摘要、退出码、耗时；`stdout` 不回写数据库。
  （审计写失败不会把已经跑完的命令变成失败请求。）
- **MCP**：`flix_shell{to, command, timeout?}` 与 CLI 共用同一实现。

流程真源：`docs/archify/flix_shell_workflow.html`（旁路 `.json`）。

### 交互式终端（`--pty`，二期）

```bash
flix shell --pty 4c4ca286                 # 默认 cmd
flix shell --pty --shell powershell 4c4ca286
```

- 目标端用 **ConPTY**（`pip install -e ".[pty]"`）分配伪终端，`vim`/`top` 这类全屏程序可用；
- 客户端把本机控制台切到 raw 模式并透传 ANSI，**Ctrl+]** 退出会话；
- **stdin 关闭即结束**（`"ver\r\n" | flix shell --pty <设备>` 这类脚本用法会自动收会话，
  不会挂着占住对端的槽位）；
- 信任门与 exec 相同（配对 + 来源 IP 一致）；缺 `pywinpty` 时明确报 `pty-unavailable`，请退回 exec；
- 空闲 30 分钟自动断开，同一时间只允许 1 个会话；握手失败按状态码给提示
  （409 = 槽位被占/旧版本泄漏，403 = 未配对或来源不符，503 = 对端缺 pywinpty）；
- 会话槽位在 teardown 抛异常时也会释放（旧版本会永久 409 直到重启对端）；
- 会话结束写一条审计（时长/退出码）。

流程真源：`docs/archify/flix_shell_pty_workflow.html`（旁路 `.json`）。

## 安全边界（2026-11 加固）

- **配对是信任边界**：`/bubble`、`/intent`（剪贴板、rdShell、rdAgent、群快照）只接受
  `pair_devices` 里已配对的发送方，且数据报来源 IP 必须与该设备记录一致；未配对或来源不符一律 403。
- **配对需要验证码**：`askPairDevice` 的 `verifyCode` 必须等于本机 4 位配对码（常量时间比较），
  无码请求在联网前就被拒绝（`pair-code-required`）。旧版官方客户端不看这个字段，但 flixcli 两端都校验。
- **Web PIN 限流**：4 位 PIN 在所有入口（`GET /<pin>`、`/validate-code`、上传、WebSocket）共享
  同一来源计数；10 次/5 分钟后限流，成功登录清零。
- **单实例**：同一 `APPDATA` 只允许一个 flixd；第二个实例以退出码 5 退出，不再抢端口/锁冲突。
- **Desktop API** 只监听 127.0.0.1，并校验 `Host`/`Origin` 与 JSON Content-Type，阻断浏览器 CSRF 与 DNS rebinding。
- **线日志默认关闭**：只有设置 `FLIX_WIRE_LOG` 才落盘，正文只记哈希，`pin=` 参数打码，单文件 5 MB 轮转。

## 回归测试

```
python tests/e2e.py         # 双节点 11 项：ping/pair/text/file/group/web/
                            # webrtc/qrcode/clipboard（需 18991/18995 空闲）
python scripts/interop_check.py --my-fp <fp> --official-fp <fp> --self-test
                            # 与原生 Flix 客户端的线格式互通（官方客户端在跑时）

# 100 轮审计（修复/回归/模糊/A-B/工具链）
python scripts/audit_rounds.py core   # R01-R50
python scripts/audit_rounds.py cli    # R51-R73
python scripts/audit_rounds.py fuzz   # R74-R83
python scripts/audit_rounds.py ab     # R84-R93（基线 worktree D:\UserData\Temp\flix-baseline）
python scripts/audit_rounds.py tools  # R94-R100
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

## 发布（多端 Release）

推一个 `v*` tag 即触发 `.github/workflows/release.yml`，四端并行构建后在
GitHub Release 一次发齐（每份附件旁边都有同名 `.sha256`）：

文件名里的 `v<ver>` 就是 tag 本身（tag `v2.6.6` → `RelayDesk-v2.6.6-win64.zip`，
**带 v**，因为 workflow 取的是 `github.ref_name`）。

| 端 | 产物 | 说明 |
|---|---|---|
| Windows | `RelayDesk-v<ver>-win64.zip` | 单文件 `RelayDesk.exe`（serve / mcp / 窗口） |
| Windows（全量） | `RelayDesk-v<ver>-win64-full.zip` | 带媒体栈（屏幕共享）；自动更新默认不取它 |
| macOS | `RelayDesk-v<ver>-macos-arm64.zip`、`-x86_64.zip` | `.app`，未签名未公证 |
| Linux | `RelayDesk-v<ver>-linux-x86_64.zip` | 单文件二进制 + zip，解压即用 |
| Android | `RelayDesk-v<ver>-android.apk` | `assembleDebug` 调试签名；密钥取自仓库密钥 `ANDROID_DEBUG_KEYSTORE_B64`，每次构建同一签名，手机才能原地升级 |

门禁：`pytest tests -q --ignore=tests/e2e.py` 全绿且 `pyproject.toml` 版本与 tag 一致，
才会进入构建；任一端缺失或失败，Release 不会创建，失败任务的 artifact 仍可下载排查。
重跑某次失败的发布用 `workflow_dispatch` 并填已有 tag，不必重新打 tag。

发布前本地流程：合入 `main` → 改 `pyproject.toml` 版本与 `CHANGELOG.md` →
`git tag v<ver>` → `git push origin v<ver>`。

Android 的门槛是签名加版本号，缺一不可：系统只允许同签名、versionCode 更大的包覆盖安装。
APK 的 versionCode 由 `pyproject.toml` 推导（`major*10000+minor*100+patch`），签名来自上面的
仓库密钥；两者任一失守，手机上的更新器下载成功也会装不上，报
`INSTALL_FAILED_VERSION_DOWNGRADE` 或 `INSTALL_FAILED_UPDATE_INCOMPATIBLE`。
已经在用的包如果来自某个已丢失的调试密钥，只能卸载重装；动手前先备份
`adb exec-out "run-as com.relaydesk.app tar -cf - -C /data/data/com.relaydesk.app ." > backup.tar`，
装完用 `run-as` 解回去（聊天库 `databases/relaydesk.db` 与 `shared_prefs` 都在里面）。

## 仓库自动化

Issue 与 PR 由仓库内的 GH Bot 处理：新 issue 自动首轮分诊，新 PR 自动评审，
**只改文档 / 测试 / 依赖锁文件**的低风险 PR 在 CI 全绿后自动批准；其余一律打
`needs-human` 标签转人工。批准规则是确定性的，与模型判断无关，配置在
`.github/gh-bot-policy.json`，说明见 [docs/GH-BOT.md](docs/GH-BOT.md)。

需要人工接手时，PR 上会出现 `gh-bot-needs-human` 标签和一条说明原因的评论；人工想阻止自动合并，加 `needs-human` 标签。

低风险 PR（仅改文档/白名单内文件、作者在白名单、CI 全绿）会被 bot 直接 squash 合并到 `main`。

## 许可与反馈

本项目以 [MIT](LICENSE) 许可发布。版本变更见 [CHANGELOG.md](CHANGELOG.md)，
安全问题的报告方式见 [SECURITY.md](SECURITY.md)。

Issues / PR 欢迎；提交前请先在本机跑一遍 `pytest tests -q --ignore=tests/e2e.py`。
