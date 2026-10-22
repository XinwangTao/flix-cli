# Flix 2.4.0 逆向取证笔记（阶段 A/C，未写业务代码）

日期：2026-10-20
目标：`C:\Program Files\Flix`（com.ifreedomer，2.4.0+127）

## 1. IDA MCP 现状

- IDA Free 9.3：`D:\IDA\ida.exe`；插件 `D:\IDA\plugins\ida_mcp.dll`（ida-pro-mcp v1.1.3）。
- MCP 端点：`http://127.0.0.1:13337/mcp`（同时提供 `/sse`），已实测 `initialize` / `tools/list` / `find_regex` 可用。
- 当前数据库：`C:\Program Files\Flix\flix.exe`（Flutter 宿主），已确认字符串：
  - `com.ifreedomer.flix/input_control`
  - `com.ifreedomer.flix/virtual_display`
  - `com.ifreedomer.flix/finder_share`
- 另一个 IDA 实例（PID 45952）未绑定 MCP 端口；需要时以 `IDA_MCP_PORT=13338` 启动并加载 `flix_rust.dll`。

## 2. 程序构成

| 文件 | 作用 | 关键证据 |
|---|---|---|
| `flix.exe` | Flutter Windows 宿主 | 版本 2.4.0+127；平台通道字符串 |
| `data\app.so` | Dart AOT 业务逻辑（24 MB） | 全部 API URL、类名、日志 `package:flix/...` |
| `flix_rust.dll` | flutter_rust_bridge 媒体核心 | `frb_*` 导出；image/exr/tiff/png/flate2/crc32fast；`frb_get_rust_content_hash` |
| `AkVirtualCamera.dll` / `ParsecVDisplay` | 虚拟摄像头 / 虚拟显示器 | 安装目录 |
| `flix-firewall*.exe` | 防火墙辅助 | Flutter assets |
| `webflix_cert.pem/key.pem` | 本地 Web 服务证书 | Flutter assets |

## 3. 端口与服务

| 端口 | 绑定 | 用途 | 证据 |
|---|---|---|---|
| UDP 8891 | 0.0.0.0 | 设备发现（组播 224.0.0.168；`multicast_group` / `multicast_port`） | netstat + 字符串 |
| TCP 8891 | 0.0.0.0 | 设备间 HTTP：`/intent`、`/bubble`、`/upload`、`/download/`、`/file` | 日志 + 字符串 |
| TCP 8892 | 0.0.0.0 | Web 客户端（偏好 `web_port`，可开 SSL） | netstat + prefs |
| TCP 8898 | 127.0.0.1 | Desktop API：`/api/*` | `[DesktopApi] serving /api/* on http://127.0.0.1:8898` |

## 4. Desktop API 路由（完整枚举）

`/api/self`、`/api/devices`、`/api/settings`、`/api/bubbles`、`/api/events`、
`/api/pair`、`/api/respond-pair`、`/api/send-text`、`/api/send-files`、
`/api/accept-transfer`、`/api/cancel-transfer`、
`/api/webrtc/offer`、`/api/webrtc/answer`、`/api/webrtc/ice`、`/api/webrtc/hangup`。

## 5. 设备间消息（已见样例）

- 剪贴板：`POST http://<peer>:8891/intent`，日志 `sendClipboard to=<id>`，响应 `{0: success}`。
- 文件气泡：`POST /bubble`，`action=TransAction.confirmReceive`，正文为
  `PrimitiveFileBubble{id, from, to, type: BubbleType.File, content: FileTransfer{...}, time, groupId}`。
- 目录传输：`_sendDirectoryStreaming` / `_sendDirComplete` / `_fetchDirManifest`（app.so 符号）。
- 文件哈希：`_computeFileHash`（Dart），Rust 侧 `frb_get_rust_content_hash`。

## 6. 账号服务（仅登录/设备列表，可选）

`https://account.flix.center/api/`：`login_api.php`、`register_api.php`、`check_email_api.php`、
`send_verification_code_api.php`、`send_forget_code.php`、`forget_password_api.php`、
`scan_login_create_api.php`、`scan_login_confirm_api.php`、`scan_login_check_api.php`、
`get_user_devices.php`、`get_profile.php`、`change_password.php`、`change_email.php`、
`logout_api.php`、`logout_device.php`、`delete_account.php`、`get_orders.php`、
`generate_payment.php`、`get_server_date.php`、`version/version.php`、`version/changelog.txt`。
支付：`https://payment.flix.center/api.php`。扫码/分享入口：`https://flix.center/qrcode/pair/r`。

## 7. 本地数据（drift/SQLite）

`%APPDATA%\com.ifreedomer\flix\db.sqlite` 表：
`pair_devices`、`notification_pair_devices`（fingerprint+code）、`persistence_devices`、
`blocked_devices`、`auto_accept_rules`、`bubble_entities`、`file_contents`、
`directory_contents`、`text_contents`、`pending_files`、`save_locations`、
`nick_names`、`file_sharing`、`mute_notification`。
偏好键包含 `device_key`、`device_pair_key`、`token`、`web_port`、`enable_web_ssl`、
`feature_visible_*`（账号/通知转发/云备份/语音/视频/投屏/远程控制/文件/扩展屏/剪贴板/搜索/虚拟摄像头）。

## 8. 待补的逆向缺口（确认后执行）

1. 发现报文格式（组播 JSON schema、广播/应答时序）。
2. 配对握手（fingerprint/code 的挑战-应答与持久化字段）。
3. `/intent` 与 `/bubble` 的完整字段与 `TransAction` 枚举。
4. `/upload`、`/download`、`/file` 的分块、哈希、断点续传与目录流式协议。
5. WebRTC 信令 payload（offer/answer/ice 的封装）与数据通道用途。
6. 远程控制的输入事件格式、屏幕编码参数与权限模型。
7. Web 客户端（8892）的鉴权与网页端远程控制路径。

## 9. 约束

- 不复制品牌、图标、文案与付费素材；不绕过 Flix MAX 会员；不接入其广告/崩溃 SDK。
- 已取证内容仅本机使用，不上传、不外发；账号 token 等敏感值不写入文档。
