# RelayDesk Android App Brief（L2）

日期：2026-10-28
状态：待用户确认（确认前不写业务代码）

## 1. 用户与问题

用户要求把这套软件装到小米平板 5 Pro 上，桌面出现真正的 App，平板与 Windows 端 RelayDesk 在局域网内直接互发消息和文件，不再依赖 ADB。

## 2. 已核实事实（带证据）

- 协议已完全取证：组播发现 224.0.0.168:8891、设备通道 HTTP :8891（`/ping_v2`、`/bubble`、`/intent`、`/file`、404 "Route not found"）、文本 bubble type 0、文件 bubble type 3、配对走 askPair/confirmPair intent（`reverse/protocol.md` §1-§8）。
- 参考实现已验证：flixcli（Python）与官方 Flix 实测互通，双节点 e2e 11/11。
- 目标设备：小米平板 5 Pro，Android 13（API 33），HyperOS；已实测组播发现、配对、文本/文件互通链路对 flixcli 可用。
- 开发环境就绪：JDK 21.0.2（E:\JAVA）、ANDROID_HOME、platforms android-34/36、build-tools 34.0.0-36.1.0；无全局 gradle（用 wrapper）；Flutter 未安装（2026-10-28 实测）。
- Android 特有约束：接收组播必须 `WifiManager.MulticastLock`；Android 10+ 后台剪贴板读写受限（本版不做剪贴板）；HyperOS `adb install` 需开「USB 安装」开关；无内置 HTTP 服务器。
- 技术栈（用户已确认）：原生 Kotlin；App 名称 RelayDesk；首版=通信基础版。

## 3. In Scope（v1）

1. 组播发现 + announce（含 MulticastLock 申请与释放）。
2. 手动 IP 添加兜底 + ping_v2 握手 + 配对（askPair/confirmPair，落本地配对表）。
3. 设备会话界面（深色、中文，对齐 RelayDesk 桌面信息架构：设备列表 + 会话气泡 + 输入栏）。
4. 文本互发（bubble type 0，本地发送即回显入库）。
5. 文件收发：发送用系统文件选择器（SAF），接收走内置 HTTP 服务器落 `Download/RelayDesk/`；复用桌面端安全门禁语义（未配对拒收、来源校验、大小上限、不覆盖已存在文件、原子落盘）。
6. 接收提示用系统通知（权限可选，拒绝只影响提示）。

## 4. Out of Scope（v1 明确不做）

- 群组与 @ 定向（桌面已支持，App 后续版本加入）。
- 剪贴板同步（Android 10+ 后台剪贴板限制，体验打折）。
- 远程观看/控制、音视频。
- 常驻后台服务：App 退后台/被杀即暂停发现与接收，打开时恢复（首版诚实边界）。
- 账号、云服务、与官方 Flix 的媒体互通。

## 5. 成功标准（可验收）

1. 平板 App 能发现局域网内的 flixd（Windows），设备列表出现别名/系统/IP。
2. 任一侧发起配对成功，双侧配对表落库。
3. 平板↔电脑文本双向送达，双方会话界面均有气泡与本地回显。
4. 电脑→平板、平板→电脑各传 1 个文件，字节一致（SHA-256），落在接收方下载目录。
5. `adb install` 安装成功、桌面出现 RelayDesk 图标；旋转屏幕不丢会话状态。
6. 对端离线时发送给出超时错误提示，不静默丢失。

## 6. 主路径与失败出口（图见 Archify）

主路径：启动授权 → 组播发现 → 配对 → 会话 → 文本/文件互发 → 接收落盘 → 回执历史。
失败出口：组播被吞→手动 IP；未配对/门禁→拒收提示；对端不可达→超时提示；权限拒绝→功能降级。

## 7. 借鉴调研（1 回合，声明外部可比项目弱）

- 借鉴本仓库 flixcli：wire 语义、错误路径、安全门禁照抄（已对官方互通验证），Android 端只换运行时。
- 借鉴 NanoHTTPD（Apache 2.0，单小依赖）做内置 HTTP 服务器；HTTP 客户端用 `HttpURLConnection` 标准库，避免引重型框架。
- 借鉴 LocalSend/KDE Connect 的「能力显式授权 + 接收目录隔离」交互（原 brief 已调研，沿用）。
- 不采用：Flutter（需装 SDK）、账号/云、常驻服务、第三方 IM 框架。
- 架构影响：App = UI(Android) + Channel(便携协议核心) + Store(本地库)，wire 格式与 flixcli 逐字节一致。

## 8. 待确认（阻塞项）

1. 确认 v1 无常驻后台：App 退后台即暂停发现与接收（要后台接收需后续加前台服务版本）。
2. 确认接收文件落 `Download/RelayDesk/` 子目录。
3. 确认 minSdk 29 / targetSdk 34（覆盖目标设备 Android 13）。
