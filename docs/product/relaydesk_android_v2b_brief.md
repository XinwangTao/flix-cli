# RelayDesk Android v2B Brief（L2）— 群组 @ 与扫码配对

日期：2026-10-29
状态：待确认（图：`docs/archify/relaydesk_android_v2b_workflow.html`）
上游：`relaydesk_android_v2_workflow.json`（v2A 已验收）；桌面群能力已存在

## 1. 用户与问题

v2A 解决了常驻与单聊，但平板上无法参与桌面已有的群聊：桌面 `flix group create/send/history` 与 MCP 群工具可用，安卓只把 `group_id` 存进消息表（`Store.kt:21,88-93`），既无群列表也无群会话页；`@` 在两端都没有任何字段或渲染（`Wire.kt:69-76`）。配对仍要手输 IP:端口与 4 位码，局域网里体验差。

## 2. 已核实事实（带证据）

- 桌面群存储：`groups(id,name,code,created_at)` + `group_members(group_id,fingerprint)`（`src/flixcli/config.py:40-50,151-163,242-263`）；CLI `group create/add/send/history`（`cli.py:435-512`）；MCP 有 `flix_group_*` 工具。
- 安卓群缺口：`Store.dedupe` 无 groups 表；`ChannelServer.kt:90-95` 仅在收消息时写 `group_id`；`MainActivity` 只有设备列表与会话页。
- `@` 无协议支持：文本气泡字段固定为 `id/from/to/type/content/time/groupId`（`Wire.kt:69-76`），桌面无 mention 解析。
- 安卓依赖仅 `org.nanohttpd:nanohttpd:2.3.1`（`app/build.gradle.kts:32-34`），无 AndroidX、无相机/二维码库；UI 为 framework Views。
- 配对链路已具备：`askPairDevice` → 目标端 6s 确认弹窗（`Runtime.kt:60-71`），接受后双侧落库；4 位码来自 `Store.ownPairKey`。
- 端口可能为 18991（v2A 回落），二维码必须带实际 `Runtime.servicePort`，不能写死 8891。

## 3. In Scope（v2B）

1. 群会话列表：群名、成员数、最后一条、未读；与单聊分区。
2. 建群：从已配对设备多选成员，本地生成 uuid 作为 groupId。
3. 群会话页：成员行、@ 选择器、文本/文件、发送状态。
4. 群消息发送：bubble 带 `groupId`；@ 用文本 `@别名`（零协议改动）。
5. 接收归组：按 `groupId` 归档，识别 `@我的别名` 高亮并升级通知。
6. 群通知：按群聚合，与单聊渠道分开。
7. 我的二维码：`relaydesk://pair?fp=...&ip=...&port=...&code=...`，端口取实际值。
8. 扫对方码：ZXing core 解码 + Camera2 预览（无 AndroidX），校验后走现有配对确认。

## 4. Out of Scope

群改名/踢人/退群的桌面 UI、红包投票、剪贴板、开机自启、账号云、离线持久队列。

## 5. 成功标准（可验收）

1. 平板建 3 人群（含 PC 与另一台安卓），三端各发 1 条，均按群归档且时间顺序正确。
2. 群里输入 `@PC别名` 发送：PC 端文本原样收到；平板/另一台安卓高亮 @ 片段。
3. 群消息在 App 退后台时按群聚合通知，点击直达群会话。
4. 收到未知 `groupId` 时落库标「未知群」，不崩溃。
5. 平板显示二维码；另一台安卓扫描后 PC 端弹出配对确认，接受后互通；拒绝则不落库。
6. 相机权限被拒 → 提示并保留手动 IP+码入口。
7. 群成员离线时消息不阻塞发送，离线成员重新上线后可见历史。

## 6. 主路径与失败出口（图见 Archify）

主路径：群列表 → 建群 → 群会话 → 发送 → 接收归组/通知。
扫码：我的二维码 ⇄ 扫对方码 → 配对确认 → 落库。
失败出口：相机拒绝→手动 IP+码；码无效→重扫；成员离线→仅投在线；未知群→标「未知群」。

## 7. 借鉴调研（1 回合，外部可比项目弱）

- 官方 Flix 闭源，无可对照实现；参考 KDE Connect / LocalSend 的二维码配对（QR 携带地址+短码，接收端仍需确认）。
- ZXing core 可脱离 AndroidX 使用：Camera2 `ImageReader(YUV_420_888)` → `PlanarYUVLuminanceSource` → `MultiFormatReader`。
- 不采用：`journeyapps:zxing-android-embedded`（强依赖 AndroidX）、Play Services Code Scanner（国内机型 GMS 不可靠）。
- 架构影响：`Store` 升 v3 增 `groups/group_members`；`MainActivity` 增群列表/群会话/扫码三块；相机逻辑独立 `QrScanActivity`。

## 8. 待确认（阻塞项）

1. `@` 用纯文本 `@别名`（推荐，官方客户端可读）还是新增 `mentions` 字段（需改 wire，官方不认）？
2. 群名如何让对端知道：随群消息带 `groupName` 扩展字段（推荐），还是对端一律「未知群」等桌面同步？
3. 二维码方向：只做手机展示+手机/桌面扫码，还是两端都要扫（PC 需相机，成本高）？

## 9. 实现与验证（2026-10-29）

确认项（用户已选）：两图都确认；@ 用纯文本 `@别名`；手机展示二维码 + 手机扫码（PC 不扫）；沿用「未知群」兜底。

改动落点：

- `android/.../Store.kt`：DB 升 v3，新增 `groups(id,name,created_at)`、`group_members(group_id,fingerprint)`、`incoming.group_id`；
  方法 `upsertGroup/addGroupMember/groups/group/groupMembers/groupConversation/groupLastMessage/groupUnread`。
- `android/.../ChannelServer.kt`：`/bubble` 读 `groupId`，未知群自动落「未知群」；消息/文件事件带 `groupId`；文件入库带 `group_id`。
- `android/.../RelayService.kt`：群消息走 `notifyGroup`（标题「群名 · 发送者」、按群聚合、群未读）；`openIntent(peer, group)` 深链；
  `clearGroupNotifications`、`hasUnreadAnywhere` 汇总摘要。
- `android/.../Runtime.kt`：`foregroundGroup`。
- `android/.../MainActivity.kt`：设备/群聊分页；群列表（成员数、最后一条、未读）；建群对话框（从已配对设备多选）；
  群会话（成员在线数、隐藏配对按钮）；群发（同一 bubbleId 并发投给在线成员，部分失败提示）；
  群文件（逐个成员上传，单人失败不中断）；`@` 选择器与 `@别名` 高亮（琥珀色）；群已读 `lastread_group_<gid>`；
  通知深链 `group`；「我的二维码」对话框（`relaydesk://pair?fp&ip&port&code`，端口取实际值）+ 手动 IP 兜底；
  相机权限回调后自动进入扫码。
- `android/.../QrScanActivity.kt`（新）：Camera2 预览 + `ImageReader(YUV_420_888)` + ZXing `PlanarYUVLuminanceSource` 解码；
  只回传原始文本，配对决策留在 `MainActivity`。
- `android/app/build.gradle.kts`：`com.google.zxing:core:3.5.3`；Manifest：`CAMERA`、`QrScanActivity`、`camera.any` 非必需。

on-device 验证（小米平板 5 Pro，192.168.236.61:5555，应用指纹 55e98322）：

1. 分页与建群：群聊页空态 → 新建 → 自动生成群名/选成员 → 直接进入群会话（「1 位成员 · 在线 1」）。
2. 群发：`@THEWANG` 一条 → 桌面 flixd 收到同 `groupId=000c97b3…`、`content="@THEWANG"`（`flix_bubbles` 实测）；
   平板气泡蓝色、无失败标记。
3. 群收：桌面 `flix_group_send` → 平板群会话即时显示「THEWANG / 群消息-echo-1」（发送者名加粗 + 消息体）。
4. 后台通知：App 退后台再收一条 → 通知「2 · THEWANG / 群消息-后台通知-2」；点击直达该群会话并清除未读。
5. 二维码：对话框正确渲染 QR，副标题「IP 192.168.137.132 · 端口 8891」（真实值）。
6. 扫码：`QrScanActivity` 打开，相机帧持续产出（logcat `CHIUSECASE` 帧号递增），无崩溃；
   预览在 `screencap` 中为黑（TextureView 常见），未做真机对码（无第二块屏幕可瞄准）。
7. 解码自测：用同款 ZXing 3.5.3 在 JVM 复现 `decode()` 的 rowStride/pixelStride 取 Y 逻辑，payload 往返一致，48ms/帧。
8. 回归：`pytest -q` 24 passed；未改桌面协议，桌面侧仅临时插入同名 gid 做收侧验证，验证后已 `delete_group` 清理。

已知限制：

- 群名/成员不同步：对端只按 `groupId` 归档，未知名显示「未知群」（官方无群同步协议，需后续定义）。
- 无群管理 UI（改名/踢人/退群）；群文件串行上传，成员多时耗时线性增长。
- 扫码预览的真机对码与相机权限拒绝路径未在本轮实测（权限拒绝已有手动 IP 兜底）。
