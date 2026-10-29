# RelayDesk Android v2C Brief：群信息同步与群管理

日期：2026-10-29 · 级别：L1 · 状态：已确认（2026-10-29，见文末决策）
图：`docs/archify/relaydesk_android_v2c_workflow.html`（旁路 `.json`）

## 事实（已验证）

- v2B（2026-10-29）已在实机验收：群收发、@ 文本、群通知、群文件、扫码配对。
- 现状缺口：群名与成员只在本机创建时存在；对端只认 `groupId`，未知名一律显示「未知群」。
- 桌面 flixcli 已有本地群：`store.groups()` / `store.group_members()` / `flix group list`，但没有任何同步入口。
- 桌面 `POST /intent` 对**未知 action 直接返回 500**（`src/flixcli/server.py:191`）——所以新增 action 必须先判定兼容性，不能假定对端接受。
- bubble 报文里的 `groupId` 是官方既有字段；本批**不改 bubble 格式**。

## In / Out of Scope

In：
- 群管理：改名、加人、移出成员、退出群（Android）
- 群信息快照 `{groupId, name, members, rev}` 经 `/intent` 广播
- 接收侧合并（rev 大者胜）与「未知群」自动补名
- 群列表随同步刷新

Out：
- 群历史消息同步
- 群主/管理员权限模型（视待决问题 1）
- 桌面群管理 UI（视待决问题 3）
- 多端并发编辑的 op-log / CRDT
- bubble 报文格式变更

## 成功标准（可验收）

1. A 改名后，B 的群列表与群会话标题在 1 次往返内变成新名字（无需重启）。
2. B 中显示为「未知群」的群，收到 A 的快照后显示真名与成员数。
3. B 加 C 入群后，C 第一次收到群消息时已能看到群名（先落本地再推）。
4. 移出成员后，被移出方发来的群消息不再进入该群会话（按其本地语义处理，见待决问题 2）。
5. 对端返回 500（官方桌面/旧版）时，本地改动仍然生效，不阻塞任何群消息收发。

## 主路径场景

- 场景 1 改名：群会话页 → 群管理 → 改群名 → 保存 → 快照逐成员投递 → 对端合并 → 双方列表刷新。
- 场景 2 加人：群管理 → 从已配对设备勾选 → 本地写入成员 → 新成员先落库再推快照 → 新成员列表出现该群。
- 场景 3 未知群补齐：收到带陌生 `groupId` 的快照 → 校验发送者 → rev 比本地大 → 补名与成员。
- 失败/人工：对端 500、成员离线、双端同时改名、伪造发送者、非 uuid 组号。

## 关键约束

- 不改 `bubble` 与 `@` 的 wire 语义；新能力只加在 `/intent` 的 `rdGroupSync` action 上（`rd` 前缀避免与官方 action 冲突）。
- 离线成员不排队：靠「下次收到群消息时回推」补齐，最多不一致一个往返。
- Android `groups` 表加 `rev` 列属 DB v4，迁移必须幂等（`onUpgrade` 里先查列）。
- 合并规则一次写死：`incoming.rev > local.rev` 才覆盖；相等保留本地并记录被丢弃快照。

## 已确认决策（2026-10-29）

1. 权限模型：对等成员 —— 任一成员都可改名、加人、移人。
2. 移出成员的本地语义：保留只读历史 —— 历史仍可读，输入框禁用，不能再发送。
3. 实施范围：Android + 桌面最小接收 —— flixcli Python 侧同时实现 `rdGroupSync` 接收与合并。

## 实施记录（2026-10-29）

- Android：Store v4（`groups.rev` / `left_at`）、`Wire.groupSyncIntent`、ChannelServer `rdGroupSync` 分支、群设置弹窗、只读态与发送闸门全部落地并在小米平板 5 Pro 实机验证（改名 / 加人 / 移人 / 退出 / 被移出 / 未知群补真名 / LWW 丢弃旧快照）。
- 桌面：`model.group_sync_intent` + `Store.merge_group_sync` + `server._group_sync_intent` 落地；`tests/test_group_sync.py` 用 aiohttp 起真实 `/intent` 覆盖成功、旧 rev、未配对、非法参数、未知 action 五种情形。
- 实现期补充：对端快照到达时若群设置弹窗正开着，立即重建（改名/成员实时刷新；被移出则整窗翻只读）。已补入图上的 note 卡片。
- 未完成：`dist/flixd.exe` 仍是改动前的构建，桌面上正在跑的 18991 守护进程对 `rdGroupSync` 仍回 500（即降级路径），需重建 exe 并重启后桌面端才真正接收。

## 借鉴 / 自研 / 不采用

- 借鉴：IM 群的「全量快照 + 版本号 LWW」是最小可行的元数据同步；比 op-log 少一个数量级的复杂度。
- 自研：`rdGroupSync` action 命名与「对端 500 即静默降级」的兼容策略，因为官方群的建群握手尚未解出（`groupchat.py` 注释已记录）。
- 不采用：CRDT / 操作日志（本批数据量小，冲突概率低）；群历史同步（体积与权限问题都太大）。
