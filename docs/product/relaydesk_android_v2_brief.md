# RelayDesk Android v2A Brief（L2）— 常驻、可靠性与体验

日期：2026-10-29
状态：已确认（2026-10-29）：按 v2A 执行；前台服务 dataSync；8891 被占回落 18991
上游：`relaydesk_android_v1_workflow.json`（v1 已验收）；桌面端 `relaydesk_discovery_workflow.json`（已确认）

## 1. 用户与问题

v1 装到小米平板 5 Pro 后，只有 App 在前台时才能收消息/文件；退后台或锁屏即断收，设备列表会把早已离线的机器一直当成在线，且 8891 被占时没有退路。v2A 解决「常驻可达 + 在线可信 + 端口自适应 + 通知/未读 + 界面打磨」，不引入群组、扫码相机与剪贴板。

## 2. 已核实事实（带证据）

- 服务绑在 Activity 生命周期上：`MainActivity.kt:85-93` 在 `onCreate` 起 `ChannelServer`，`:258-268` 在 `onPause/onDestroy` 停发现、停服务 → 进程回收后无法接收。
- 端口写死 8891：`Wire.kt:13`、`MainActivity.kt:86`（服务端）、`:334`（手动添加提示）、`:348-349`（默认端口）；`Discovery.kt:27` 监听 8891。若平板上官方 Flix 已占用 8891，App 只能弹「端口被占用」。
- 在线状态没有时间语义：`Store.kt:41-56` 的 `devices()` 不返回 `seen_at`；`MainActivity.kt:314` 只要记录里有 IP 就展示，重启后历史设备看起来仍在线。
- 通知只在进程存活时发出：`ChannelServer.kt:291-344`；文本通知固定 id=3（后到覆盖先到），无按会话聚合；未读仅靠列表文字变色（`MainActivity.kt:310-318`），无数字角标。
- 入站配对默认接受：`ChannelServer.kt:151-168`，`askPairDevice` 在 `verifyDevice` 匹配时直接落库，没有用户确认步骤。
- 数据库版本 1 且 `onUpgrade` 为空（`Store.kt:11,28`）：新增表/字段必须补迁移，否则老用户升级会崩或缺列。
- v1 安全门禁保留：未配对拒收、来源 IP 校验、大小上限、原子落盘（`ChannelServer.kt:192-289`），本批不改语义。

## 3. In Scope（v2A）

1. 前台服务常驻：`ChannelServer` + `Discovery` + 探活循环移入前台服务；通知栏常驻；App 退后台/锁屏仍可收。
2. 端口自适应：优先 8891，占用时回落 18991；announce 广播**实际**端口；对外连接使用对端 announce 端口（已有）并补候选端口探测。
3. 在线判定与探活：`seenAt` 90s TTL；每 60s 对库内设备 `/ping_v2` 探活刷新；离线灰显 + 最后在线时间。
4. 通知与未读：按设备聚合通知（`setGroup`），点击直达对应会话；列表显示数字未读数；前台服务常驻通知独立渠道。
5. 会话体验：最后一条消息 + 时间、未读角标、下拉刷新、空态；文件接收进度与「打开」。
6. 配对确认：入站 `askPairDevice` 先弹确认（默认拒绝），确认后落库；保留 4 位配对码。
7. 发送可靠性：失败重试 3 次（0.5s/1.5s/4s 退避），失败消息标红可重发；不做离线持久队列（明确边界）。

## 4. Out of Scope（v2A 明确不做）

- 群组与 @ 定向（v2B，单独 L1 图）。
- 相机扫码配对（v2B；本批保留手输 IP/码）。
- 剪贴板发送（Android 10+ 后台限制）、远程观看/控制、音视频。
- 开机自启、账号与云、与官方 Flix 的媒体互通。

## 5. 成功标准（可验收）

1. App 退后台并锁屏 5 分钟，PC 发文本与文件，平板仍即时收到并通知；手动杀进程后不再接收（诚实边界写入 UI）。
2. 列表在线点符合 90s TTL：断网 90s 内变灰并显示最后在线时间；恢复后 60s 内重新变绿。
3. 平板先装官方 Flix 占用 8891 时，RelayDesk 自动落 18991，PC 仍能发现并互发。
4. 两条会话各收 3 条消息：通知按设备分组、未读数正确、点击通知进入对应会话。
5. 接收 1 个 ≥10MB 文件：进度可见，完成后可点开；SHA-256 与发送端一致。
6. 未授权通知权限时：功能可用，未读数仍在应用内可见。
7. 入站配对未确认时不落库；确认后双侧互通。

## 6. 主路径与失败出口（图见 Archify）

主路径：启动授权 → 前台服务 → 发现/端口 → 在线判定 → 会话列表 → 会话页 → 收发 → 通知未读。
失败出口：8891 被占 → 回落 18991；组播被吞 → 手动 IP；通知权限拒绝 → 应用内未读；对端离线 → 重试 + 失败标记；进程被杀 → 明示不再接收。

## 7. 借鉴调研（1 回合，声明外部可比项目弱）

- 借鉴 KDE Connect / LocalSend 的常驻连接通知与「按对端聚合」通知交互；不引入其插件体系。
- 借鉴 Android 官方 FGS 指南：`targetSdk 34` 必须声明 `foregroundServiceType`；本批用 `dataSync` 并如实说明系统可能限时。
- 自研：wire 与 flixcli 逐字节一致、端口回落与探活策略对齐桌面端 `presence.py` / `netcheck`。
- 不采用：WorkManager 轮询（延迟大）、WebSocket 常连（协议不含）、第三方 IM SDK。
- 架构影响：`MainActivity` 只做 UI，连接与接收全部下沉到 Service；`Store` 升到 v2 并补 `onUpgrade`。

## 8. 待确认（阻塞项）

1. 第一批范围是否就按 v2A（常驻 + 在线 + 端口 + 通知未读 + UI），群组/@ 与扫码放 v2B？
2. 前台服务类型用 `dataSync`（实现简单，Android 15+ 可能限时），还是 `connectedDevice`（更贴合外部设备连接，但需额外权限与商店说明）？
3. 端口回落目标是否就定为 18991（与桌面 flixd 默认一致），还是同时探测 18998/19098？

## 9. 实现与验收记录（2026-10-29）

实现：`RelayService.kt`（新）承接发现/接收/探活/通知，`MainActivity.kt` 退化为 UI；`Store` v2 增 status；`ChannelServer` 入站配对改确认制；`ChannelClient.uploadFile` 增进度回调。

- 已实机验收（小米平板 5 Pro，192.168.236.61:5555）：FGS 常驻（`isForeground=true`，通知「设备通道 :8891 · 后台接收已开启」）；退后台收文本入库 + 按设备通知 + 数字角标；通知点击直达会话并清角标；文件接收 63B 内容一致、通知「收到文件」；8891 被占时自动落 18991（`/proc/net/tcp` 仅 4A2F，/ping_v2 回 18991）；入站配对弹窗、接受后双侧落库；下拉刷新监听触发；`am kill` 不杀 FGS。
- 发送重试：文本消息首发 + 3 次重试（0.5s/1.5s/4s，重试超时 5s）；`status` 1=发送中、2=失败可点红气泡重发。已实测正常路径 status 0；失败/退避路径仅代码走查，未构造断网场景（关 Wi-Fi 会断 adb TCP，风险大）。
- 边界：文件发送失败仅标红，重发=重新选文件（未做自动重传）；无离线持久队列。
- 同机多指纹：桌面 prefs 重置会生成新 uuid（本机实测 `%APPDATA%\flixcli` 4c4ca286、嵌套 `flixcli\flixcli` 2e05614d、更早 c4330534），安卓侧已修：探活按 `/ping_v2` 实际返回的 fingerprint 落库（`RelayService.probeDevices`），列表按「alias+ip」去重（在线 > 已配对 > 最新 seenAt，`MainActivity.dedupeDevices`）。实测旧指纹行不再被刷成在线、列表只留一行。
- 测试辅助：`com.android.adbkeyboard/.AdbIME` + `im`/`input text` 注入中文输入，事后切回讯飞；拉库用 `adb exec-out run-as com.relaydesk.app cat databases/relaydesk.db`（`run-as cp` 到 /sdcard 会被拒）。

