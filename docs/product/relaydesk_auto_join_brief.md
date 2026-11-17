# RelayDesk 自动发现与官方互通 Brief

状态：待确认（图：`docs/archify/relaydesk_auto_join_workflow.html`）
日期：2026-11-17

## 要解决的问题

1. 手动输入 IP 才能添加设备，换网/换端口就失效。
2. 自动发现实测在 Mac 侧失效：旧版 `/Applications/flix.app` 占用 UDP 8891，RelayDesk daemon 绑定失败后**连广播也一起跳过**（日志 `discovery unavailable; skipping announce`），导致手机看不到 Mac；反向也没有主动扫描兜底。
3. 自身多端（Mac/Windows/Android）的文本、文件、图片能力已有，但缺一份可重复的端到端验收与断网/重连兜底。
4. 与官方 Flix Android App（`com.ifreedomer.flix` 2.4.4）的协议已逆向（`reverse/protocol.md`），需对当前版本重验并纳入自动发现。

## 目标（可验收）

1. 同网段两端启动后 ≤10s，设备列表自动出现对方，无需输 IP、无需点刷新；**8891 被占用时同样成立**。
2. 首次通信前保留一次配对确认；配对后文本、文件、图片双向成功。
3. 验收矩阵：
   - RelayDesk Mac ↔ RelayDesk Android：文本 / 文件 / 图片（3/3）
   - RelayDesk Mac ↔ Flix 官方 Android 2.4.4：文本 / 文件 / 图片（3/3）
4. 失败可读：跨网段、组播被 AP 屏蔽、8891 被占、官方版本不兼容，都有明确提示与手动 IP 兜底。

## 范围

**做**：广播发送与监听绑定解耦；8891 冲突降级（主动 LAN 扫描 `8891 + ping_v2`）；两端自动入库/去重；配对入口一键化；官方互通回归（`scripts/interop_check.py`）；图与测试。
**不做**：改官方 App；公网中继；无确认的自动配对（默认不做，见待确认）；Windows 实机验收（本机无设备，仅代码兼容）。

## 关键约束

- 官方 wire 固定：UDP 224.0.0.168:8891 发现、TCP 8891 `/ping_v2` `/bubble` `/intent` `/file`；不能要求官方端升级。
- 广播 payload 的 `port` 只标注设备通道端口，发现报文 dst 端口必须仍是 8891。
- macOS 15 本地网络权限：adb 仍需 loopback 中继；RelayDesk 进程的局域网收发需实测放行。
- Android 编译走 GitHub Actions `release.yml`（本机无 SDK/JDK），改动需可被 CI 构建。

## 借鉴调研（1 回合，≤15 行）

- 借鉴：KDE Connect / LocalSend 均为「广播/mDNS + 主动探测」双通道——只靠被动广播在网络设备睡眠、组播被屏蔽时会漏设备；主动探测是必要兜底。
- 自研：官方 Flix 不支持 mDNS，查询/应答也不存在，故不能照搬 mDNS 作为唯一发现方式；保留 8891 组播 + 广播，新增 TCP 主动扫描。
- 不采用：公网发现/中继、无确认自动配对（安全边界）。
- 对架构影响：发现层加「发送与接收解耦 + 扫描兜底」；配对与信任层不变。

## 待确认（≤3）

1. 自动配对策略：推荐「自动发现入库，首次通信前一键确认配对」；零点击自动配对需你明确同意。
2. 官方互通范围：只 Android 官方 App（本机可实测），还是也要覆盖官方桌面端？
3. 8891 冲突策略：推荐「仍广播 + 主动扫描」共存；或启动时提示并引导只保留一个 daemon。
