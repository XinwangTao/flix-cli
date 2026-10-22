# Flix CLI 克隆项目 Brief（L2）

日期：2026-10-20
状态：待用户确认（确认前不写业务代码）

## 1. 用户与问题

用户本机已安装并日常使用 Flix 2.4.0（com.ifreedomer / 江河智连），需要一套**功能对等、全部能力可 CLI 调用**的替代实现，用于自动化与脚本集成（设备发现、收发文件、剪贴板、远程控制等）。

## 2. 已核实事实（带证据）

### 2.1 程序构成

| 组成 | 文件 | 证据 |
|---|---|---|
| Flutter 宿主 | `C:\Program Files\Flix\flix.exe`（840 KB，2.4.0+127） | 版本资源；`com.ifreedomer.flix/*` 平台通道 |
| Dart 业务逻辑 | `data\app.so`（24 MB，AOT 快照） | 全部 API URL、类名、日志来源 `package:flix/...` |
| Rust 媒体核心 | `flix_rust.dll`（1.5 MB，flutter_rust_bridge） | `frb_*` 导出；image/exr/tiff/png/flate2/crc32fast 依赖，`frb_get_rust_content_hash` |
| 原生能力插件 | `input_control`、`virtual_display`、`finder_share` 平台通道 | IDA 数据库字符串（0x14009cfe0/0x14009d568/0x14009db80） |
| 第三方组件 | flutter_webrtc、screen_capturer、clipboard_watcher、AkVirtualCamera、ParsecVDisplay、内置防火墙 | 安装目录 DLL/EXE 清单 |

### 2.2 已确认的功能面（来自 shared_preferences 功能开关）

账号、通知转发、云备份、语音通话、视频通话、投屏（screen_cast）、远程控制、文件互传、扩展屏（extended_display）、跨设备剪贴板、搜索、虚拟摄像头。另有 Flix MAX 会员与广告/更新模块。

### 2.3 网络与协议（已取证）

- **设备发现**：UDP 组播，组播地址 `224.0.0.168`（字符串 `multicast_group` / `multicast_port` / `MulticastSocketUtil`）；本机实测 UDP 8891 已绑定。
- **设备间 HTTP（TCP 8891）**：`/intent`（剪贴板等意图）、`/bubble`（消息/文件气泡）、`/upload`、`/download/<id>`、`/file`。日志实测 `http://<peer-ip>:8891/intent`、`/bubble`。
- **剪贴板消息**：`sendClipboard` → POST `/intent`，响应 `{0: success}`。
- **文件气泡示例**（日志原文）：`PrimitiveFileBubble{id, from, to, type: BubbleType.File, content: FileTransfer{name, mimeType, nameWithSuffix, size, path, progress, state, waitingForAccept, receiveBytes}, time, groupId}`，外层 `action = TransAction.confirmReceive`。
- **Web 客户端**：TCP 8892（偏好项 `web_port`），内置 `webflix_cert.pem` / `webflix_key.pem`，门户 `flix.center/web`。
- **本机 Desktop API**：仅绑定 `127.0.0.1:8898`，路由已完整枚举：
  `/api/self`、`/api/devices`、`/api/settings`、`/api/bubbles`、`/api/events`、`/api/pair`、`/api/respond-pair`、`/api/send-text`、`/api/send-files`、`/api/accept-transfer`、`/api/cancel-transfer`、`/api/webrtc/offer|answer|ice|hangup`。
- **WebRTC 信令**：offer/answer/ICE/hangup（Desktop API 与设备通道配合），用于通话/投屏/远程控制。
- **账号服务**（仅账号相关，非局域网必需）：`account.flix.center/api/*`（登录、注册、扫码登录、设备列表、资料、改密、注销、版本、验证码、订单）与 `payment.flix.center`。

### 2.4 本地数据模型（drift/SQLite，`%APPDATA%\com.ifreedomer\flix\db.sqlite`）

- 设备：`pair_devices`、`notification_pair_devices`（fingerprint + code）、`persistence_devices`、`blocked_devices`、`auto_accept_rules`
- 消息：`bubble_entities`、`file_contents`、`directory_contents`、`text_contents`、`pending_files`
- 配置：`save_locations`、`nick_names`、`file_sharing`、`mute_notification`

### 2.5 运行时现状（只读观察）

- Flix 正在运行（PID 12516）：TCP 8891/8892、127.0.0.1:8898、UDP 8891。
- 日志显示用户已有多台配对设备，剪贴板双向同步、文件气泡传输均为局域网内明文 HTTP。

## 3. 范围（建议，待确认）

### In Scope（目标交付）

1. `flixd` 常驻守护进程：发现、配对、设备通道服务端/客户端、传输、剪贴板、WebRTC、本机能力层。
2. `flix` CLI：所有能力可命令行调用，支持 `--json`、脚本化退出码与进度输出。
3. 协议文档与逆向取证归档（IDA MCP 分析结果、Dart/日志取证、消息样例）。
4. 与官方 Flix 客户端的互通能力（范围取决于确认项 Q3）。
5. Windows 优先；架构保留跨平台边界。

### Out of Scope（建议不做）

- 不复制 Flix 商标、图标、文案、付费素材；界面/输出为自有实现。
- 不绕过 Flix MAX 会员/付费与广告体系。
- 不接入其广告/崩溃统计 SDK。
- 不自建账号后端（除非确认选择“完全自建”）。

## 4. 成功标准（可验收）

1. 同一局域网两台 Windows：`flix discover --json` 能列出对端 fingerprint/别名/IP/端口。
2. `flix pair` 成功后，`flix text`/`flix send` 可发送文本与文件（≥1 GB，含进度、取消、断点续传），接收端 `flix recv` 可接受/拒绝。
3. `flix clipboard --sync` 双向剪贴板同步。
4. `flix remote` 可查看对端画面并注入键鼠；`flix cast` 可投屏/扩展屏。
5. `flix call --voice/--video` 双向音视频通话。
6. 若选互通：官方 Flix 客户端能发现、配对并完成文本/文件/剪贴板互传。
7. 所有命令有稳定 `--json` 输出与文档化退出码。

## 5. 借鉴调研（1 回合，GitHub）

- **LocalSend**：局域网组播发现 + HTTPS 传输，协议开放、无账号。借鉴其发现与传输的健壮性；但为互通必须遵循 Flix 自有 JSON/HTTP 协议。
- **KDE Connect**：JSON 插件式消息 + TLS 配对。借鉴“能力声明 + 插件路由”的组织方式，对应 Flix 的 intent/bubble 双通道。
- **RustDesk**：rendezvous/relay + 输入注入 + 画面编码。借鉴远程控制的失败兜底（直连失败转中继）与输入注入抽象。
- **Sunshine/Moonlight、Deskreen**：低延迟串流与浏览器投屏。借鉴扩展屏/网页观看的降级路径。
- 对架构的直接影响：① 发现/配对/通道分层，便于兼容官方协议；② 能力插件化，CLI 子命令即能力；③ 远程控制必须有“直连→中继/仅观看”的失败出口。

## 6. 待确认（阻塞项）

- **Q1 后端策略**
  - A. 兼容官方（推荐）：CLI/守护进程遵循已取证的局域网协议，账号用于登录与设备列表；可与官方客户端同网共存。
  - B. 完全自建：自研账号、发现、信令与中继，不依赖 `flix.center`；与官方客户端不互通。
  - C. 双轨：默认官方兼容，另留自建后端开关。
- **Q2 首批必须功能（多选）**：文件/目录传输、剪贴板同步、远程控制、语音/视频通话、投屏/扩展屏、通知转发、云备份/搜索。
- **Q3 互通要求**：必须与官方 Flix 客户端互通（推荐，决定协议保真度）/ 仅自研端互通 / 先自研后兼容。

确认后进入阶段 D：先补齐 §8 的协议缺口（IDA MCP + 运行时取证），再按图实现 `flixd` 与 `flix` CLI。

