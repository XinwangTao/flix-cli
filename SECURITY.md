# 安全策略

## 报告漏洞

请**不要**在公开 Issue 里提交安全问题。用 GitHub 的
[私密漏洞报告](https://github.com/XinwangTao/flix-cli/security/advisories/new)，
或在 Issue 里只写「有安全问题，请联系我」并留下联系方式。

请附上：影响版本、复现步骤、涉及的端口/接口、是否已在两台真机上复现。

## 支持范围

只对最新 Release 和 `main` 分支的最新提交提供修复。历史版本不回补。

## 设计上的边界（已知且非漏洞）

- `flixd` 与官方 Flix 客户端同处一个局域网时，任何能发起 announce 的设备都可被列为「已发现」；
  **配对**才是信任边界，未配对设备不能投递消息、文件或 `rdAgent` 信封。从 v2.2.1-hardened 起，
  已配对还要来源 IP 与设备记录一致（`pair_devices` + `persistence_devices.ip`），两者缺一即 403。
- **配对码是必需输入**：`askPairDevice` 的 `verifyCode` 必须等于本机 4 位配对码，否则返回失败；
  CLI/MCP 在发起网络请求前就拒绝无码调用。配对成功不会因为「对方声称已配对」而自动追加其他设备。
- Web 界面 4 位 PIN 在 `GET /<pin>`、`/validate-code`、上传与 WebSocket 入口共享限流计数
  （10 次/5 分钟，按来源 IP），暴力枚举会先撞上 429/跳转，而不是无限次尝试。
- 同一个 `APPDATA` 只允许运行一个 flixd（`DAEMON.LOCK`）；第二个实例直接退出，避免端口与
  SQLite/偏好文件竞争。
- Desktop API（`127.0.0.1:8898`）校验 `Host`/`Origin` 与 JSON Content-Type，阻止网页通过
  DNS rebinding 或简单表单提交驱动本机 API。
- 线日志默认不落盘；仅当显式设置 `FLIX_WIRE_LOG` 时记录，正文只存哈希，`pin=` 打码并 5 MB 轮转。
- `scripts/fix_firewall.ps1` 需要管理员权限，只在你显式执行时放行本地端口，程序不会自动运行它。
- 媒体面（`/webrtc/v1/*`）只接受已配对来源、校验来源 IP、nonce/seq 与 HMAC；不接公网中继。
- 自动化应答（`flix agent`）只回**已配对**设备，并带前缀过滤、最小间隔与最大跳数护栏，
  不会形成无限对喷。

## 模型共享网关（`flix models`，默认端口 18993）

- **默认关闭。** 全新安装时 `enabled=false`，不监听任何端口；要共享必须显式开启
  （设置页「模型共享」或 `flix models start`）。
- **只监听局域网地址，不监听公网**；地址由本机网卡决定，程序不做端口映射、不接外部中继。
- **必须带令牌。** 首次开启自动生成一个随机令牌，之后可用
  `flix models token --reset` 轮换（轮换即时生效）。接受的两种发送方式：
  `x-api-key: <token>` 或 `Authorization: Bearer <token>`。
  401 的回复刻意**不区分**「令牌错」与「令牌格式不对」，避免成为令牌校验预言机。
- **只开放三个端点**：`POST /v1/messages`、`POST /v1/chat/completions`、`GET /v1/models`，
  外加只读的 `/health` 与自检页 `/`。其它路径一律不处理。
- **令牌在界面上打码**（首 6 位…末 4 位）。面板可能出现在屏幕共享画面里，明文会直接泄给
  看的人；要完整值请用「复制手机配置」或「显示连接二维码」。
- **错令牌按来源限流**：同一来源 60 秒内 10 次 401 后锁 30 秒（429 + `Retry-After`）。
- **上游并发闸门**：同时打到上游的数量默认压到 5，其余在本地排队——这不是安全控制，
  是为了不把 cc-switch 打崩。
- **级联中继**（`upstream` 指向另一台 RelayDesk）：转发时用的是**对端令牌**
  （`upstream_token`），客户端自己的令牌不会下发给上游；`allow_remote_upstream` 未开启时
  拒绝把上游指向局域网内其它机器。
- **自检页的令牌在 URL 查询串里**（`/?token=...`），方便手机浏览器直接打开；这是有意的
  设计权衡（页面本身就是持令牌者的工具），但别把这个 URL 贴到公开地方。

## 凭据

仓库与发布产物不含任何密钥或令牌。网关令牌是**运行时生成**、只存在本机的
`shared_preferences.json` 里，不会进版本库。
若你发现提交历史里有明文凭据，请按上面的私密渠道报告，不要自行改写历史。
