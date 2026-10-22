# flix-cli

Flix 2.4.0+127（com.ifreedomer 江河智连）局域网互操作复刻：`flixd` 守护进程 + `flix` CLI +
`flix mcp`（AI 插件接口）。以 IDA MCP 静态逆向 + 线上 verbatim 取证为基准实现
（协议规格与证据标注见 `reverse/protocol.md`），与官方 Flix 客户端实测互通。

```bash
git clone https://github.com/XinwangTao/flix-cli.git
cd flix-cli
python -m venv .venv
.venv\Scripts\pip install -e .        # Windows
# source .venv/bin/activate && pip install -e .   # Linux/macOS

flixd                       # 常驻：设备通道 8891 + Desktop API 8898（默认）
```

与官方客户端同机共存：`flixd --device-port 18991 --api-port 18998`。

发现组播默认走系统路由选中的网卡，并跳过回环/APIPA 与 **/32 虚拟网卡**（VPN 网关如 `vgate0`
常是 /32，会抢走组播路由导致 announce 发到无对端的虚拟链路）。需要指定时用
`--multicast-if 192.168.1.23`（`flix discover` / `flixd` / `flix serve` 均支持），或设
环境变量 `FLIX_MULTICAST_IF`；启动日志会打印实际使用的网卡。

announce 同时以**局域网广播兜底**（子网定向 + 255.255.255.255，端口不变）：部分企业安全代理
（Tencent iOA、Sangfor 等内核 callout）会在协议栈内吞掉全部出站多播（pktmon 实证），广播是
这类网络上唯一能出卡的 announce；flixcli 间发现不受影响，官方客户端是否接受广播未证实。

## 功能矩阵

| 功能 | CLI | MCP 工具 | 官方互通 |
|---|---|---|---|
| 发现（UDP 224.0.0.168:8891，启动突发 11 包） | `flix discover [--announce]` | flix_discover | ✅ |
| 探测 | `flix probe <ip[:port]>` | flix_probe | ✅ 404 "Route not found" |
| 手动添加（ping_v2 握手） | `flix ping <ip[:port]>` | — | ✅ 官方 UI 添加成功 |
| 配对（askPairDevice/confirmPairDevice） | `flix pair [--code]` | flix_pair | ✅ 双侧 pair_devices 落库 |
| 文本气泡（type 0） | `flix send-text` | flix_send_text | ✅ |
| 剪贴板投递（TransIntent clipboard） | `flix clipboard` | flix_send_clipboard | ✅ |
| 文件传输（bubble + POST /file，share_id=bubbleId，.flixdownload 原子写） | `flix send` | flix_send_files | ✅ 配对后官方自动收 |
| 剪贴板自动同步（官方 sendClipboard 语义） | flixd 内置（`--no-clip-sync` 关） | — | ✅ 官方实测到达 |
| 群组（本地群表 + bubble+groupId 逐发） | `flix group create/add/join/leave/list/send/history` | flix_group_* | ✅ 官方可收 |
| 配对二维码 | `flix qrcode [--decode]`（flix2:// payload） | — | flixcli 间 |
| Web 发送页（validate-code→/&lt;pin&gt;→ws+/upload） | `flixd --web-port 18992` | — | 协议对齐官方 8892 |
| WebRTC 信令面（offer/answer/ice/hangup） | REST `/webrtc/signal`、`/api/webrtc/*` | — | flixcli 间 [compat] |
| Desktop API（self/devices/settings/events SSE/send-text） | `http://127.0.0.1:8898` | flix_status | 形状对齐 |

## AI 接入（MCP）

`flix mcp` 是 stdio JSON-RPC MCP server。客户端配置：

```json
{ "mcpServers": { "flix": { "command": "D:\\flix-cli\\.venv\\Scripts\\flix.exe",
                            "args": ["mcp"] } } }
```

工具（15）：flix_self / flix_discover / flix_devices / flix_probe / flix_send_text /
flix_send_clipboard / flix_send_files / flix_bubbles / flix_group_list /
flix_group_create / flix_group_add / flix_group_send / flix_group_history /
flix_pair / flix_status。

局域网多设备 AI 通讯：每台设备跑 `flixd` + `flix mcp`，AI 之间即可互发文本/剪贴板/
文件/群消息：

```
flix_status → flix_discover → flix_send_text <fp> "…"      # 点对点
flix_group_create "ops" → flix_group_add ops <fp2> …        # 多设备群
flix_group_send ops "广播指令"                               # AI 群发
flix_bubbles                                                # 读收件
```

## 回归测试

```
python tests/e2e.py         # 双节点 11 项：ping/pair/text/file/group/web/
                            # webrtc/qrcode/clipboard（需 18991/18995 空闲）
```

## 退出码约定

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
- WebRTC 媒体面（aiortc）未接，信令面为 flixcli 间 [compat]；官方信令
  TransAction 真名待解。
- 二维码 payload 为 flixcli 自有 `flix2://` 格式（官方编解码待解）。
- 合规约束：不复制品牌资源、不绕过付费、不上传敏感数据。
