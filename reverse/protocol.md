# Flix 2.4.0 协议规格（IDA MCP 逆向 + 线上抓包/Oracle 验证）

状态：核心通道已确认；文件分块/配对/WebRTC 待续（§8）。
标注方式：
- `[IDA app.so@13338 0xADDR]` = IDA MCP 静态分析（数据库 `D:\flix-cli\reverse\ida\app.so`）
- `[RT]` = 运行时取证（抓包 capfull2.pcapng / 官方日志 / oracle）
- `[?]` = 待逆向

分析管线（本仓库 reverse/ 下，全部经 IDA MCP 驱动）：
`textscan.py`（MCP get_bytes 扫 .text 全部 `[r15+disp32]` 对象池加载，128k 条）
→ `mempool.py/pool_resolve.py`（读活进程解析池条目/池基址）
→ `pool_full.py`（全池字符串解析 + 加载映射，5+4 个池、~20 万条映射）
→ `func_map.py/func_strings.py`（函数级字符串清单）。
关键事实：Dart AOT ELF 的 .rodata 是序列化流（无绝对指针），必须经
「活进程池解析 + 静态指令扫描」两侧合并才能 disp→字符串。

## 1. 设备发现（UDP 组播 224.0.0.168:8891）——已确认

- announce 载荷 = JSON 字符串（双重编码）：
  `"{\"deviceModal\":{\"alias\":…,\"deviceModel\":\"Windows\",\"deviceType\":\"desktop\",\"fingerprint\":\"<uuid>\",\"port\":8891,\"version\":127,\"ip\":\"\",\"host\":\"\",\"from\":\"\",\"os\":\"windows\"}}"`
  [RT capture_discovery.txt 252B×11]；字段集与 IDA 完全一致
  [IDA app.so@13338 池 0x224e09bc3f1 含 deviceModal/alias/deviceModel/deviceType/fingerprint/port/version/ip/host/from/os]
- 语义：**仅启动时突发 11 个**；无周期 announce；无查询/应答机制；
  无 mDNS 服务（zeroconf browse 仅见打印机）[RT]。
  对运行中的对端，发现靠持久化设备探测（TCP 8891 GET / → 404 "Route not found"）。
- 未观察到官方对我们 announce 的任何应答 [RT]。
- flixcli 实现附加（非官方行为）：announce 在组播之外同发局域网广播
  （子网定向 + 255.255.255.255，dst 端口仍 8891）。动机：部分企业安全代理
  （Tencent iOA / Sangfor 等 callout 驱动）会在协议栈内静默吞掉**全部出站
  IPv4 多播**（pktmon 实证 Tx=0，单播/广播不受影响）[RT 2026-10-22]；
  flixcli 的发现 socket bind 0.0.0.0:8891，广播可正常兜底（flixcli↔flixcli）。
  官方客户端是否接受广播 dst 未证实（官方 db 未见广播探针落库）。

## 2. 设备通道（TCP 8891，明文 HTTP/1.1，Dart/3.10 UA，shelf_router）——已确认

| 路由 | 方法 | 请求 | 成功响应 |
|---|---|---|---|
| `/intent` | POST | TransIntent JSON（见 §4） | 200，字面量 `{0: success}`（Dart map toString，非 JSON）[RT] |
| `/bubble` | POST | 气泡 JSON（见 §3） | 200 `received bubble`（纯文本）[RT] |
| `/upload` `/download/<id>` `/file` | — | [?]（ShipService，下一轮） | — |
| 其他 | * | — | 404 `Route not found`（纯文本）[RT] |

解析失败：500 `receive intent failed`（栈：TransferIntentHandler.receiveIntent →
TransIntentMapper.fromJson，dart_mappable）[RT 日志 verbatim]。

## 3. 气泡（Bubble）——已确认（收发双向 verbatim）

```json
{"id":"<uuid>","from":"<fp>","to":"<fp>","type":0,"content":"<text>","time":<ms>,"groupId":""}
```
- type：0=Text（db 分布 0/1/2/3；3=File [RT 日志 `type: BubbleType.File`]）。
- 发送方收到 200 `received bubble`；接收方入库 bubble_entities + text_contents
  （oracle T3 + db 验证）[RT]。
- FileTransfer content 字段（日志 toString）：resourceId,name,mimeType,nameWithSuffix,
  size,path,width,height,thumbnailBase64,androidContentUri,extra,progress,speed,
  state,waitingForAccept,receiveBytes；FileState 观察值 waitToAccepted/inTransit。
  键集存在于池 0x224e0fa5e19 / 0x224e09cde11 [IDA app.so@13338]。
- TransAction 观察值：`confirmReceive`、`cancelReceive`、`clipboard`
  （日志 `_receiveIntent action = TransAction.clipboard` verbatim）[RT]；
  相关方法名 sendFileManagerAccept/Reject/Disconnect、sendCameraShareRequest
  [IDA app.so@13338 函数 0xcf258d 邻域]。

## 4. TransIntent（/intent 载荷）——已确认（线上抓包 + oracle 双向验证）

capfull2.pcapng verbatim（官方客户端 → 对端（Peer-A），剪贴板同步）：
```json
{"deviceId":"<sender-fp>","bubbleId":"","action":"clipboard",
 "extra":{"text":"{\"text\":\"flixd-probe-20261021C\",\"time\":1792554971152}"}}
```
- `deviceId`=发送方 fingerprint；`bubbleId` 剪贴板场景为 ""；
- `action`="clipboard"；`extra.text` 是**再次 JSON 编码**的 `{"text":"…","time":<ms>}`；
- 成功响应体：`{0: success}`；`_receiveIntent` 日志 `action = TransAction.clipboard,
  bubble = null extra = {text: …}` [RT 日志]；
- **未配对设备同样被接受**（oracle：伪造 deviceId 亦 200 并应用剪贴板）[RT]。
- intent 池：0x224e0f948b1（含 data/action/intent/fingerprint/clipboard/type 等
  [IDA app.so@13338]）。

## 5. Desktop API（127.0.0.1:8898）——已确认

- `GET /api/self`：`{"id","alias","deviceModel","deviceType","ip":"","port","online":true,"paired":false}`
- `GET /api/devices`：设备数组（含 id/alias/deviceModel/deviceType/ip/port/online/paired）
- `GET /api/settings`：`{"autoReceive","saveDir","theme","enableMdns"}`
- `GET /api/events`：SSE，`retry: 2000`，`data: {"type":"devicesChanged","payload":[…]}`
- `POST /api/send-text` → `{"ok":true}`（未观察到对应 /bubble 出网，投递路径 [？]）
- 其余路由 /api/pair|respond-pair|send-files|accept-transfer|cancel-transfer|
  webrtc/offer|answer|ice|hangup（请求体 [?]）

## 6. Web 客户端（TCP 8892）

- 启动日志 `Serving at http://<ip>:8892  PIN: <4位>`（每次启动随机）[RT 日志]；
  `POST /validate-code {"code":"<pin>"}` → 200 空体 [RT]；
  首页为 514KB 内联 HTML/JS（仅 PIN 登录逻辑）。
- 鉴权流 verbatim：`POST /validate-code {"code":"<pin>"}` → 200 → **重定向
  `GET /<pin>`**；429=错误过多（30 分钟锁）。主应用（/2938 实测，20KB）=
  简化发送页：`WebSocket ws://<ip>:8892/ws`（纯文本消息；服务端可推
  `refresh` 指令）+ `POST /upload`（XHR 文件上传，带进度/速度 UI）。
  无 WebRTC、无完整远控。[RT 2026-10-21 拉取分析]

## 7. flix-cli 实现对照（本批已交付）

| 官方事实 | flix-cli 代码 | 验证 |
|---|---|---|
| announce 格式/突发 | `discovery.py build_announce` | 解析器对官方 announce parse=True [RT] |
| 404/500/200 响应语义 | `server.py` | curl 实测三态一致 |
| /bubble 文本收发 | `model.text_bubble` + `channel.post_bubble` + `server.bubble_post` | CLI→官方 200 received bubble 入官方库 [RT] |
| /intent TransIntent | `model.clipboard_intent` + `channel.post_intent` + `server.intent_post` | CLI→官方 200 {0: success} 剪贴板生效；官方→flixd 同样生效 [RT] |
| Desktop API 形状 | `desktop_api.py` | /api/self、/api/devices、/api/settings、/api/events（SSE）实测 |
| 探测 | `cli.py probe` | 对官方对端与 flixd 均识别 flix:true |
| 群消息 = bubble+groupId 按成员逐发 | `groupchat.py` + `config.py groups 表` + `cli group *` + MCP `flix_group_*` | 官方端入库 group_id 完整；双 flixcli 节点双向收发实测（2026-10-21） |
| 文件气泡+POST /file 传输 | `files.py`（bubbleId=resourceId）+ `server.py /file`（.flixdownload 原子写）+ `cli send` + MCP `flix_send_files` | node2→node3 落盘逐字节一致；官方端自动接收（配对后 2026-10-21 20:01 实测落盘） |
| 手动添加握手 ping_v2 | `model.device_modal` + `channel.ping_v2` + `server /ping_v2` + `cli ping` | 官方 UI 添加 node2 成功（2026-10-21 17:06）；官方 ping 回真身份 |
| 配对握手 askPairDevice/confirmPairDevice | `model.pair_intent` + `channel.pair_device` + `server _pair_intent` + `cli pair` | `flix pair 127.0.0.1:8891` success，双侧 pair_devices 落库；官方→node3 confirmPairDevice 应答正常 |
| 接收方自动 confirmReceive | `model.confirm_receive_intent` + `server bubble_post` | node2→node3 自动链验收（22:2x 落盘+state 入库） |
| 剪贴板共享确认（notification-pair） | `server _pair_intent` confirm 分支 | 官方广播 confirmPairDevice 应答（官方剪贴板同步到达 node3） |
| 群命令通道 | `server group_command_post` + `store.group_by_code/set_group_code` | resolve 200 应答实测（官方侧静默丢弃=应答格式待解，§8） |

## 8. 群聊协议（请求格式已抓 verbatim，应答待解）

- **群消息 = 普通 bubble + groupId，按成员逐发**（官方接受带 groupId 的
  bubble → 200 received bubble）[RT db/oracle]。
- **加入群第一步 = `POST /group/v1/command`** body verbatim：
  `{"action":"resolve","code":"<4位群号>"}` —— 「加入群组」UI（输入四位群号）
  对每个在线设备广播 resolve；创建方应答群信息，其余设备
  404 `{"error":"找不到群组"}` [RT wire_capture 21:41/21:56/22:03]。
- 应答格式待解：flixcli 回平铺 {groupId,groupName,members,...} 与 data 包裹
  形均被官方**静默丢弃**（对话框关闭、无报错、无入群）[RT]。GroupMessage 键集
  sender/text/content/data/command/action/members/groupName/groupId（池
  0x26f6b3d29f9，新进程基址 0x21ef5ad29f9 由 pool_rebase.py 4 锚验证）；fromJson
  键加载不走常规 [r15+disp32] 形态（扩展 textscan 指令形态或真 MAX 创建方抓包
  方可续解）。
- **创建群组 = Flix MAX 付费功能**（本机 vipRemainingDays=-13 → 弹付费墙）
  [RT UI]；群状态为**内存态**（官方 db 无 group 表、transfer_groups 空，重启即失）
  [RT db/fs]。
- 路由全集（shelf_router 正则，池枚举）：`^/ping_v2$`、`^/file$`、
  `^/dir_manifest$`、`^/heartbeat$`、`^/speedtest$`、`^/thumbnail$`、
  `^/group/v1/command$`、`^/group/v1/file$`、`^/group/v1/thumb$`——**无 pair
  路由**（配对走 /intent，见 §10）。
- 群命令 action 字符串族：resolve/create/sync（0x1398b88-0x1398dc5
  GroupCommandMapper 候选）；WebrtcProcessor/GroupCallProcessor 源路径串在
  池 0x21ef5a9fac1（rebase 2 锚验证）。
- /group/v1/file、/group/v1/thumb 依赖群建立，待解。

## 9. 文件传输协议（已解出，抓包 verbatim 2026-10-21）

发送（对任意 Flix 兼容接收端）：
1. `POST /bubble` type=3，content（**嵌套，无 path/hash/extra**，verbatim）：
   ```json
   {"state":2,"progress":0.0,"speed":0,"receiveBytes":0,
    "meta":{"name":"x.txt","mimeType":"text/plain","nameWithSuffix":"x.txt",
            "androidContentUri":"","size":8,"width":0,"height":0,
            "resourceId":"<uuid>"},
    "waitingForAccept":true}
   ```
   state 为 int 枚举（2=picked；官方日志另见 waitToAccepted/inTransit；
   db file_contents.state 见 5/6）。groupId 用 null。
   **bubble.id 与 meta.resourceId 必须同值**（接收端按 share_id 直查 bubble，
   [RT `_receiveFile shareId=... bubble=null ... should not null`]）。
2. 等接收方 confirmReceive intent（已配对设备自动发，`{0: success}` 应答）后
   `POST /file?share_id=<resourceId>&file_name=<name>&mode=Instance+of+%27FileMode%27`
   到同一对端：`content-type: application/octet-stream`、`transfer-encoding: chunked`
   裸文件体；接收方 200。share_id 即 meta.resourceId（=bubbleId）。
3. DesktopApi 触发（官方→任意）：`POST /api/send-files` 参数 **`{"deviceId":"<fp>","paths":["..."]}`**
   （键不是 to/fingerprint/peerId；日志 `to=` 只是打印标签）→ FileSendQueue 派发。
- 接收端武装条件：**已配对设备才自动接收**（TransAction.confirmReceive）；
  未配对发送方的文件气泡会入库并在 UI 等手动接受——/file 提前到达会
  `_receiveFile ... bubble = null` → 500 [RT 日志 verbatim]。
- 接收方确认流（官方日志 verbatim 22:15）：`_receiveBubble json===> {完整气泡}`
  → 对已配对发送方 **confirmReceive 重试 5 次**（`confirmReceive: device <fp>
  not found yet, retry N/5`；离线放弃 `confirmReceiveFile failed: Bad state:
  Device <fp> is offline or not found`）→ 发送方在线时 confirmReceive intent
  到达（`<bubbleId> confirm receive: response: {0: success}`）。
- **TransAction 成员再+4**：`sendFileManagerRequest / sendFileManagerAccept /
  sendFileManagerReject / sendFileManagerDisconnect`（文件管理握手族）
  [RT 日志 action= verbatim]。
- **bubble.type 全集 = {0=Text, 1=Image, 2=Video, 3=File}**（历史日志 verbatim：
  `type:1`+`BubbleType.Image`；type=2+FileTransfer content 实测 200 received
  bubble [RT oracle 22:3x]；type=1/2 携带 string content 才 500——Image/Video
  与 File 一样需要 FileTransfer 结构 content）。
- **FileState 线上协议**：发送方只发 **state=2 (picked)**；waitToAccepted/
  inTransit 是收发双方本地状态（官方日志 toString），不上线。生命周期 verbatim：
  picked →（接收方 confirmReceive）→ waitToAccepted → inTransit → done。
- **share_id = bubble.id 双重实锤**（接收端日志 `_saveFileAndAddBubble`：
  meta.resourceId 为空、share_id=<bubbleId> 完成传输 [RT 2026-05-15]）。
- **接收端原子写**：传输中写 `<name>.flixdownload`，完成后呈现最终名
  [RT 2026-05-15]；flix-cli server.py /file 已对齐（os.replace）。
- FileTransfer toString 新版含 `hash: null` 字段（content 可带 hash，非必需）。
- 官方 add-device 日志 verbatim：`_receivePingV2 from "{...}"`（body 双重编码）
  + `add device success DeviceModal(alias:..., deviceType: DeviceType.desktop,...)`。
- 官方间应答同为 `received bubble`（`发送成功: response: received bubble`）。
- **FileState（气泡 content.state）**：名称族 {waitToAccepted, picked,
  inTransit, cancelled}（日志 toString verbatim 152/44/43/1 次）；发送方构造
  **2=picked**。db file_contents.state 是另一个枚举（5=picked、6/8=inTransit，
  db×log 交叉取证），不可混用。
- flix-cli 接收侧已实现官方语义：bubble_post 对已配对发送方自动回
  confirmReceive intent（model.confirm_receive_intent：deviceId=接收方、
  bubbleId=气泡 id、extra={}）；node2→node3 配对+自动收文件验收通过
  （22:2x，文件落盘+state 入库）。

## 10. 配对（pairing）——第一步握手已解出（ping_v2，2026-10-21 wire verbatim）

**手动添加设备第一步 = `POST /ping_v2`** [RT wire_capture.log 16:41:46/48/56，官方重试 3 次]：
- 请求体：`{"deviceModal":{"alias":…,"deviceModel":"Windows","deviceType":"desktop",
  "fingerprint":"<发送方fp>","port":<用户输入的对端端口>,"version":127,
  "ip":"","host":"","from":"manual","os":"windows"}}`
  （结构=UDP announce 的 deviceModal；`from:"manual"` 标记手动添加）
- 响应（oracle 判别实验 ×4 确认）：
  - 有效 body → `200` `text/plain; charset=utf-8`，body = **服务器自己的 deviceModal
    双重编码**（与 announce 同构的 json 字符串再包一层 json 字符串）：
    `"{\"alias\":\"DESKTOP-A\",…,\"fingerprint\":\"<peer-fp>\",\"port\":8891,
    \"version\":127,\"from\":\"manual\",\"os\":\"windows\"}"`——注意响应是裸 modal
    字段（不带 deviceModal 外层）；`from` **回显请求的 from**（zzz→zzz 实测）；
    请求体其余内容被忽略（发假 fingerprint 也回真身份）
  - 空 deviceModal `{}` → `400` "Bad Request"（plain text）；最小字段
    `{fingerprint,alias}` 即可 200
- handler：`package:flix/domain/ship_server/processor/ping_v2_processor.dart`
  （池 0x224deb9fac1 slot 855959；路由正则 `^/ping_v2$` slot 35567）[IDA app.so@13338]
- **添加=双向自我介绍入库**：官方把请求体 deviceModal 原样入库
  persistence_devices（oracle 假身份 X/from=zzz、m/abc 均被入库 [RT 官方 db]）；
  发起方入库响应、接收方入库请求。`from` 字段=发现来源标记
  （multi=组播 / manual=手动）。添加成功=单次 ping_v2，无后续请求、无重试
  [RT wire_capture 17:06:29]。
- flix-cli 实现（本批）：`model.device_modal/ping_v2_body/parse_ping_v2_response` +
  `channel.ping_v2`（客户端）+ `server.py /ping_v2`（服务端）+ `flix ping`（CLI）。
  实测：对官方 8891 ping 回真身份；flixd 对官方请求 200/400 三态一致；
  **官方 UI 手动添加 node2 成功**（2026-10-21 17:06）。
- 添加失败的根因（已修）：此前 flixd 对 /ping_v2 落 404 "Route not found"，
  官方 UI 判定添加失败。

**配对（pair_devices）与添加是两层** [RT 官方 db 2026-10-21]：
- ping_v2 添加只写 persistence_devices；pair_devices 不变（仍只有历史两台
  移动设备 0ab9bf4e/adc2d7dd，code 均 ''）。
- 「自动连接」信任开关（设备卡片 ⋯ 菜单→设备设置）写 auto_accept_rules
  `(device_id, feature='auto_connect')`，不写 pair_devices；自动收文件
  全局开关+分设备「文件」勾选也在同表（feature 维度区分）。
- 桌面端 UI 无配对入口：「添加更多设备」对话框仅 IP+端口两字段（无配对码
  输入框）[RT UI 截图]；设备卡片右键无菜单；设备设置面板仅信任开关。
- 配对子系统函数族（池 0x224e0fa5e19，PairDeviceService 大函数
  ≈0x11139C4-0x1120D7C）[IDA app.so@13338 反查+disasm]：
  `askPairDevice`/`confirmPairDevice`/`cancelPairRequest failed`/
  `pairDevice askPairDevice net`/`notify peer to unpair failed: `/
  `device_pair_key`/`SELECT fingerprint FROM pair_devices WHERE fingerprint = ?`
  ——发起=askPairDevice、接受=confirmPairDevice（命名与 TransAction.confirmReceive
  同族，但均非 TransAction 成员 → 配对消息不走 /intent action）。
- 扫码配对 URL：`https://flix.center/qrcode/pair/<payload>`（池串；payload
  格式待解——桌面 UI「View the pair code」入口未定位，设置/设备设置/+菜单/
  设备卡片右键均无）[IDA app.so@13338 + RT UI 探索]。

**配对握手全案（/intent，2026-10-21 RT oracle + 官方日志 verbatim）**：
- **TransAction 成员含 `askPairDevice`/`confirmPairDevice`**（此前测的
  pair/pairDevice 等短词不是成员；完整 camelCase 方法名才是）——配对走
  /intent。日志：`action = TransAction.askPairDevice, bubble = null extra = {...}`
- 发起 A→B：`POST /intent {"deviceId":"<A fp>","bubbleId":"",
  "action":"askPairDevice","extra":{"verifyDevice":"<B fp>",
  "verifyCode":"<B 的4位码>"}}`
  → B 校验 verifyDevice==本机 → **写 pair_devices[A_fp, code=<B 自己的
  device_pair_key>]** → 响应 `{0: success}`
- 回执 B→A：`confirmPairDevice extra={verifyDevice:"<A fp>",verifyCode,
  accept:true}` → 同样写库/确认。
- 响应为 Dart map toString：`{0: success}`；`{301: pair Device Null}`
  （verifyDevice 缺失）；`{302: device not match return  verifyDevice = X
  did = Y}`（verifyDevice != 接收方本机 fp；did=接收方 fp，双空格 verbatim）。
- **verifyCode 实际未被校验**（v2.4.0+127 实测：错码 0000 同样 success 入库）
  [RT oracle]。
- 边角（官方 quirk）：deviceId 不在 persistence_devices 的全新 fp 发
  askPairDevice → success 且**把本机 fp 写入 pair_devices**（code=verifyCode）
  [RT db]——主路径（先 ping_v2 添加）无此问题。
- `pair_devices.code` 列=**写库方自己的配对码**；历史两行 code='' 为免码配对。
- 日志 verbatim（handler）：`askPairDevice verifyDevice = X verifyCode = Y
  askCode = null  askDevice = null` / `confirmPairDevice verifyDevice = X
  verifyCode = Y accept=true`；askCode/askDevice=反向链字段（待解）。
- **配对收益 E2E 实证**：配对后官方自动接收文件——node3（新身份）pair →
  `flix send` → 官方 Downloads 自动落盘、内容逐字节一致 [RT 2026-10-21 20:01]。
  链路：发送方 POST /bubble → 接收方（已配对）自动回
  confirmReceive intent（`<bubbleId> confirm receive: response: {0: success}`）→
  发送方 POST /file。
- **关键修正（§9）**：官方 bubbleId 与 meta.resourceId 为**同一 uuid**——
  接收端 `_receiveFile shareId=<...>` 直接按 share_id 查 bubble
  （`Bad state: ... should not null`）[RT 日志 verbatim]；二者不同值时 /file
  500。flix-cli files.py 已改为同 uuid。
  confirmReceive intent 的 **bubbleId 字段携带气泡 id**（非空，verbatim：
  `{"deviceId":"<接收方>","bubbleId":"<bubbleId>","action":"confirmReceive",
  "extra":{}}`）[RT wire_capture 20:01:51]。
- **剪贴板共享确认 UI（notification-pair 分支）**：官方弹「<设备> 想要与本机
  共享剪贴板 [拒绝/允许]」；点允许 → 官方对**每个在线设备**广播
  `_replyPairDecision`：`POST /intent {"deviceId":"<官方fp>","bubbleId":"",
  "action":"confirmPairDevice","extra":{"accept":"true",   ← 字符串！
  "verifyDevice":"<官方fp>","verifyCode":"2199"}}`。日志 verbatim：
  `_replyPairDecision from = <目标fp> verifyDevice = <官方fp> verifyCode = 2199
  accept=true` + `剪贴板配对请求回复成功: response: {…}`。**官方不检查响应
  body 错误码**（302 也算"回复成功"）。接收方语义：写 pair_devices[官方fp]。
  注意坑：我们的主身份在官方库 port=8891 → 官方广播打到 8891=自环
  （官方自己 _receiveIntent→success，自己写自己）[RT 日志]。
- flix-cli 实现（本批）：`model.pair_intent/parse_intent_reply` +
  `channel.pair_device`（客户端）+ `server.py` intent_post 的
  askPairDevice/confirmPairDevice/confirmReceive 分支（服务端，302 格式
  verbatim 对齐）+ `flix pair <ip[:port]> [--code]`（ping_v2 拿身份 →
  askPairDevice → 双侧写库）+ `store.own_pair_key()`（prefs
  device_pair_key，4 位随机）。实测：`flix pair 127.0.0.1:8891 --code 2199`
  → success，官方 pair_devices 出现 flixcli 行。
- 其余池串：`PairDeviceService`、`PairDevicesDao`、`PairCodeProvider`、
  `PairCodeContentComponent`、`Enter pair code`、`Pair code`、
  `Paired devices`、`View the pair code`、`pair_device_menu`、
  `PairInfo{ips: `、`PairFailureCause.`、`[PairDiagnostics] cause=`、
  `_replyPairDevice from = `、`_replyPairDecision from = `（intent 池）、
  `notification_pair_devices` 表（剪贴板/通知配对，独立于 pair_devices）。
- 待解：askPairDevice/confirmPairDevice 的网络载体（候选：/bubble type 1/2
  ——db type 分布 0/1/2/3 中 1/2 未知；或专属路由）、二维码 payload、
  配对码（device_pair_key）在哪一步校验。

已确认（配对状态存储侧）：
- 本机 pair_devices：`(fingerprint, code='', time)`——LAN 配对无每对一码；
  prefs `device_pair_key='2199'`（4 位，本机配对码）；auto_accept_rules 存
  `auto_connect`。
- **TransIntent action 枚举（/intent 实测成员）**：`clipboard, text, file,
  notification`（confirmReceive/cancelReceive 亦可通过 action 解码）。
  `pair/unpair/pairDevice/connectDevice/pairConnect/handshake/connect` 全部
  不是成员（MapperException `No enum value matches` 计数法）→ **配对不走
  /intent**。
- paircode 子系统（池串，IDA@13338）：`paircode_add_IP/_port/_manually/
  _success/_scan_to_add`、`pairCode`、`pair_router_handler.dart`——UI 流程为
  手动输入对端 IP+端口+配对码添加设备。
- DesktopApi `POST /api/pair`：任意 body 都返回 `{"ok":true}` 且无日志/无副作用
  ——无 web 会话时是哑接口。
- 接收门禁：文件自动接收（confirmReceive）仅对 pair_devices 中的设备生效。
待解：ping_v2 成功后官方是否立即写 pair_devices（还是需要后续握手/配对码
验证）；paircode 在何时被校验（添加对话框里输入的码 vs 本机 device_pair_key）。

## 11. 待逆向清单（2026-10-21 深夜状态）

已完成（本轮）：配对全案（§10：ping_v2 添加 + askPairDevice/confirmPairDevice
握手 + 剪贴板共享广播）、confirmReceive 自动链（flixcli 实现+双节点验收）、
Web 客户端全案（§6）、群 resolve 请求格式、bubble type 全集 {0,3}、
FileState 名称族、设备通道路由全集、TransAction 成员 14 个。

剩余（按阻塞原因）：
1. 群 resolve **应答**格式 —— 阻塞：需真 MAX 创建方（同一 LAN 的另一官方客户端，群号未知）
   或扩展 textscan 指令形态后解 GroupMessage.fromJson（键加载非标准
   [r15+disp32] 形态）
2. /group/v1/file、/group/v1/thumb —— 依赖群建立（同上）
3. WebRTC 信令 TransAction 真名 —— webrtc-offer/ice/initwebrtc 等候选均非
   成员 [RT oracle]；WebrtcProcessor 函数绑定待续（0x21ef5a9fac1 池已 rebase）
4. 二维码 payload（flix.center/qrcode/pair/）—— 阻塞：桌面 UI 无「查看配对码」
   入口（移动端功能）
5. askPairDevice 的 askCode/askDevice 反向链字段；/group/v1/command 其余
   action（create/sync）载荷
6. FileState / db transfer-state 的 int 全表
7. 远程控制输入事件 + 扩展屏/虚拟显示（flix.exe native sub_14000C4A0/
   sub_1400124B0，IDA@13337；需先建 WebRTC 会话，前置=3）
8. sendFileManagerRequest/Accept 的 extra 载荷（正式文件握手与简化
   bubble+/file 路径的关系）

工具链状态：官方 20:45 重启后 PID=24724；池基址 rebase——GroupMessage=
0x21ef5ad29f9（pools7_new.json）、ship 路由池=0x21ef5a9fac1（pools8_ship.json）；
pool_rebase.py（锚串字节搜索+numpy 反查指针+多键验证）为通用重定位工具；
code_loads.json 静态扫描与进程无关可直接复用。
