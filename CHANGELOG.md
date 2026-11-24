# 变更记录

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；版本号与 GitHub Release 标签一致。
可执行产物见 [Releases](https://github.com/XinwangTao/flix-cli/releases)。

## [未发布]

### 修复
- rdShell 远程执行现场两个故障：
  - **失败只剩裸 500**：目标端执行器抛异常时 aiohttp 回 `500 … Server got itself in
    trouble`，发起方分不清是「对端构建过旧 / 环境缺 COMSPEC / 进程起不来」。现在
    `/intent` 回 `{ok:false, error:"shell-failed", detail:"<异常类型>: <信息>"}`，daemon
    stderr 落完整 traceback；`remoteshell.run_command` 起进程失败改为返回 `exit=127` +
    `stderr` 原因，不再把异常抛给 handler。
  - **PTY 单会话槽位永久泄漏**：`finally` 里先 `session.close()` 再 `active_ptys -= 1`，
    pywinpty 在子进程已死时抛异常就跳过了递减，此后每次 attach 都是 409 busy，只能手工
    重启对端。改为先递减，`close`/审计/`ws.close` 各自 suppress，并加 `watch_detach`
    在 socket 断开时主动关 ConPTY（同时放出被阻塞的读线程）。
- `flix shell --pty` 客户端：stdin 关闭（管道/脚本用法）不再原地空等，直接发
  `{"t":"close"}` 结束会话；握手 409/403/503 分别给出可操作提示，不再是「连接失败: 409」。
- 审计写库失败不再把已经执行成功的命令变成失败请求（只丢那一行审计）。
- **Windows 上 `os.kill(pid, 0)` 会杀进程**：`adb_wireless._pid_alive` 用它探活，
  而 CPython 在 Windows 把非 CTRL_C/CTRL_BREAK 的信号直接转给 TerminateProcess。
  后果是 `stop_relay` 里的「存活检查」把目标进程杀掉（陈旧 pidfile 还会误杀复用了
  该 pid 的无关进程），`tests/test_scan_connect.py` 也因此把整个测试进程带走。改为
  Windows 走 `OpenProcess(SYNCHRONIZE)` + `WaitForSingleObject(0)`，POSIX 保持 `os.kill(pid, 0)`。
  （顺带说明：Windows 上 `stop_relay` 仍读不到命令行，验证不过就拒绝停止并保留 pidfile。）

### 新增
- `tests/rd_shell_integration.py`：双真实 daemon 的 rdShell 联调（exec / 链式命令 /
  退出码透传 / PTY 客户端被强杀后槽位仍可复用）。
- `tests/test_remoteshell.py`、`tests/test_ptyshell.py` 增补回归：执行器异常返回结构化
  错误且不写审计、teardown 抛异常后槽位仍可复用。

### 其他修复
- 界面一直闪：毛玻璃背景原先用 DWM_CLOAK 把窗口藏起来再截屏，窗口每 3 秒真的
  消失一次。改用 WDA_EXCLUDEFROMCAPTURE（截图排除窗口、屏幕上看不出变化），
  刷新从 3 秒放宽到 30 秒，移动/缩放停下 400ms 后再补抓。
- 手机端玻璃：国产 ROM 常拒绝第三方应用读取系统壁纸（getDrawable/peekDrawable
  都返回 null，给权限也一样），因此补了一条不依赖壁纸的自绘极光底（主题色系
  径向渐变 + 高斯模糊），叠加半透明面板后与桌面观感一致，不再是死黑屏。
- 调试包改用独立 applicationId（com.relaydesk.app.debug），可以和正式包共存，
  验收玻璃效果时不必卸载重装、不丢聊天与配对。
- Windows 玻璃材质回来了：DWM 的 Acrylic 对 Qt 分层窗口只会"设置成功但什么都不画"，
  而旧代码一拿到 True 就关掉自绘模糊，结果整个界面变成实心灰。现在 Windows 明确走
  自绘模糊背景（桌面截图 + 高斯模糊 + 半透明面板），并每 3 秒用 DWM_CLOAK 把自己藏
  起来重抓一次桌面，移动窗口、换壁纸都能跟上，不再是一张启动时的死图。
- Android 端补上玻璃底：壁纸模糊层（API 31+ 用 RenderEffect，29/30 用缩图放大兜底）
  垫在最下面，屏幕与面板改成半透明色板，两端口径一致。
- 主动发现改成常驻：每 60 秒扫一遍本网段 8891，并在扫描后向所有候选 IP 单播公告。
  以前只在启动时扫一次，官方客户端又只在启动时公告，所以中途上线的设备、以及换了
  网络的设备永远发现不了。
- 端口开着但不应答我们握手的设备不再被丢弃：现在按「其它版本/官方客户端」列进设备
  列表（`unidentified`），任意版本的 Flix 都能看见。

- 组播网卡不再被虚拟适配器抢走：Wi-Fi 一闪断，`default_iface_ip()` 就会挑中 VMware 的
  VMnet1（192.168.73.1/24，无网关）或 Hyper-V 的 vEthernet，announce 全打进没有人听的
  虚拟链路，本机在手机设备列表里凭空消失，从手机发起的配对自然配不上。现在按
  GetAdaptersAddresses 的 FriendlyName/Description 过滤 VMware/VirtualBox/Hyper-V/
  Wi-Fi Direct/Bluetooth/Wintun/VPN/Clash 一类适配器，并把已选网卡钉住（只有它真的
  消失才换），避免每次轮询重新猜。
- `flix netcheck` 打印被跳过的虚拟适配器（地址 + 适配器名）；`shared_preferences.json`
  的 `multicast_if` 可手工钉死网卡，GUI 启动的 daemon 也能生效。
- 桌面端配对超时从 6s 放宽到 32s（`PAIR_TIMEOUT`）：对端可能不在屏幕前，需要等它的
  通知提示，6s 会让一次本来能成功的配对看起来失败。
- Android 设备身份不再随机重铸：指纹缺失时由 ANDROID_ID 派生（同一签名 + 同一设备
  恒定）并用 `commit()` 落盘。清数据/重装后手机不再是"新设备"，旧配对、会话线程和
  设备列表不会再对不上（同一 IP 出现 5 个 PKJ110 指纹就是这个原因）。
- Android 后台也能配对：没有可见 Activity 时不再立刻回 `301 pair Device Null`，改为弹
  高优先级通知（接受/拒绝按钮，25s 有效），把请求挡在 HTTP 线程里等用户点。

## [2.6.4] - 2026-11-19

### 新增（自动连接）
- 自动连接信任开关（设置页「自动连接：发现同网段新设备就直接配对」）：发现的未配对设备
  自动发起 `askPairDevice`（`extra.auto=true`），对端在身份核验通过后直接写配对，两端都
  不用点确认。开关存在 prefs `autoConnect`（`enabled` 全局 + `devices` 分设备覆盖），
  Android 侧读 `auto_connect`，默认开启；关掉后回到原来的人工确认流程。
- 自动连接的身份门槛不是"它说自己是谁"：必须是我们已经在该 IP 发现过的设备，且
  `/ping_v2` 在源地址上答出的指纹与之一致才会写配对；伪造指纹、同 IP 的其它应用都不会
  被自动信任。核验放在后台任务里跑，ask 本身立即返回 `301`，不占对端 8s HTTP 预算。
- 模型共享（网关）开机自动拉起：上次开着共享的应用重启后会自己恢复监听，不再出现
  "已开启但未运行"、对端连不上却看不出原因的状态。

### 修复
- 配对超时默认值放宽到 `PAIR_TIMEOUT=32s`（对端可能只弹通知、要 25s 内响应）；
  GUI 手动配对的 HTTP 超时从 30s 抬到 45s，避免 daemon 还在等确认时报失败。
- 自动连接开关与 daemon 同源：开关写 `shared_preferences.json`（此前误写
  `~/.relaydesk_gui.json`，关掉后 daemon 仍自动配对）。
- 自动配对只认带 `extra.auto` 的请求：官方客户端的手动配对仍弹确认。
- 主动方写配对前也 `ping_v2` 核验指纹；Android 侧同样要求核验通过才跳过确认。
- 构建日志不再入库（`build*.log`、`dist/` 加入忽略）。

### 版本（2.6.4）
- 源码版本号抬到 2.6.4，避免更新器把 v2.6.3 当作更新覆盖本地构建。

### 版本（2.5.3）
- 版本号抬到 2.5.3：此前源码停在 2.4.0，macOS 更新器会把 11-10 的旧发布 v2.5.2 当成新版
  装进来，等于把当天所有修复回退掉。

### 修复（对抗性审查，2026-11-17）
- 配对确认异步化：快速路径最多等 6s；超时后立即返回 `301`，审批通过再由桌面端补发
  `confirmPairDevice`。此前同步等待 30s 超过手机端 8s HTTP 超时，一对一键配对必然报失败。
- 首次来信确认前先 `ping_v2` 核验发送方指纹，防止同一 IP 上的其他应用伪造 `from` 触发配对弹窗。
- 主动扫描改指数退避 + 抖动（启动 2s 首扫；命中后退到 120s/900s，未命中 5→10→…→上限），
  不再每 60/300 秒固定扫全网段。
- 设备去重比较忽略 `source`/`from` 等瞬态字段，消除扫描与广播交替造成的重复入库和日志抖动。
- GUI 增加构建指纹握手（`/api/overview` 的 `build`）：升级后不再复用旧 daemon；本机服务 API
  端口被旧实例占用时自动顺延，GUI 仍能发现新实例。

### 新增（Android 自动更新）
- 开机广播与应用启动时检查 GitHub `releases/latest`，发现新版本发通知；Wi-Fi 下自动下载、
  校验 sha256，再交给系统安装器（Android 不允许静默安装，确认后保留数据）。
- 设置页新增「检查更新」；应用内发现已下载的新版本时弹出安装提示。
- 发布/调试工作流统一使用同一份 debug keystore Secret；Release 用 tag 写
  `versionName/versionCode`，保证升级可比较、可覆盖安装。

### 变更（第三轮对抗性审查：删除常驻扫描）
- 永久扫描调度器删除：启动后 2s 扫一次，之后由 GUI 刷新按钮触发
  `POST /api/discover/scan`。新设备靠周期广播；官方端晚于我们启动时会发启动广播，
  早于我们启动则由启动扫描发现。少了一个永动机和一类陈旧窗口 bug。
- 配对确认显示对方 4 位验证码；手机「我的配对二维码」同时显示该码，便于核对后再允许。
- 设备通道非 8891 时 GUI 明确提示「官方 Flix 互通不可用」，不再静默降级。

### 修复（第二轮对抗性审查）
- 升级生命周期：GUI 启动新构建前，发现旧构建 daemon（build/version 不匹配）即按 `pid`
  终止（旧版无 `pid` 时用端口反查），避免旧 daemon 占着单实例锁与 8891 继续服务。
- 审批后补发 `confirmPairDevice` 时，先按请求源地址 + `ping_v2` 解析对端**当前** IP/端口，
  不再信任缓存；手机在 8891/18991 间切换、或 DHCP 换地址也能送达。
- 设备列表改按（地址, 别名）去重：同一手机上的官方 App 与 RelayDesk 两个身份都可见；
  同别名的重复安装仍折叠为最新一条。
- 扫描上限回到 300s（移动端换 IP/端口后最多 5 分钟恢复），并把当前设备端口纳入扫描目标；
  `pendingPairs` 附带设备别名。
- 启动器等待 8891 真正释放后再选端口；GUI 发现 daemon 停在 18991 且 8891 空闲时自动迁回，
  避免抢跑后永久失去官方 Flix 互通。

### 新增（自动发现与一键配对）
- 发现发送与监听解耦：UDP 8891 被其他进程占用时仍照常广播（只发不收），并记录降级原因；
  监听侧尝试 `SO_REUSEPORT`，可与旧实例共存绑定。
- 主动 LAN 扫描兜底：启动 2s 后首扫，对同网段 TCP 8891/18991 发 `ping_v2` 校验身份；
  之后按指数退避补扫（见上方修复），官方端只在启动广播也能被发现。
- 手机发起的一键配对不再因「未携带本机配对码」直接拒绝：GUI 弹「允许/拒绝」，
  超时后异步补发确认（见上方修复）；`POST /api/pair/approve` 供无界面环境使用，
  `/api/overview` 新增 `pendingPairs`。

### 新增（ADB 一键无线连接）
- ADB 页新增「一键无线连接」：`POST /api/adb/connect` → mDNS 发现 → 回环中继 →
  `adb connect`，无需扫码或手输地址。
- 设置页与 ADB 页的二维码统一成 `relaydesk://pair`（配对 + 模型配置），不再出现只能
  在浏览器打开的模型地址码。
- 说明：Android「无线调试 → 使用二维码配对设备」是**电脑显示、手机扫**，载荷必须是
  `WIFI:T:ADB;S:<一次性服务名>;P:<6位码>;;`（见上一条 `flix adb pair-qr`）。

### 新增（macOS 自动更新）
- `flix update` 支持 macOS `.app`：能识别 bundle 为目标、按 CPU 架构选资产（Intel 取
  `x86_64`、Apple Silicon 取 `arm64`，此前会误选另一架构）、整包原子替换并保留备份。
- zip 解包时还原**可执行位**与**符号链接**：PyInstaller 的 `Contents/Frameworks/Python`
  等链接若被解成普通文件，新装应用会在启动时报 "slice is not valid mach-o file"。
- 更新前停止正在运行的实例，更新后 `open -a` 拉起并用 `--version` 验收，不健康则回滚。

### 新增（模型网关加固）
- 并发上限：默认 4 个在途请求，超出直接 `429 overloaded_error`（带 `Retry-After: 2`），且不
  缓冲请求体，避免一个持有令牌的客户端打满本机 cc-switch；`flix models start --max-concurrent`
  可调（1–32），`models status` 会显示当前值。
- 失败可诊断：网关保留最近 20 条拒绝（时间 / 原因 / 来源地址），`/health`（凭令牌）与
  `flix models status` 都能看到最后一条，便于判断是令牌过期还是并发打满。
- 端口无人监听时立即返回「未监听」，不再等满探测超时。
- 含网关令牌的 `shared_preferences.json` 落盘权限改为 `0600`。
- `/api/pair-link` 的健康探测移到线程池执行：1.5s 的同步探测原先会卡住整个 daemon 事件循环。
- 手机端设置页新增「模型网关」项：显示地址与掩码令牌，可复制配置、测试连通、手动填写。

### 新增（macOS 实时毛玻璃）
- macOS 窗口默认启用毛玻璃材质：每 300ms 用 CoreGraphics 截取「本窗口之下」的屏幕内容
  （`CGWindowListCreateImage` + below-window），再在 Qt 内模糊合成，窗口移动/背后内容变化
  都会实时跟随；`QScreen.grabWindow` 会把窗口自己拍进去，所以不用它。
- `GET /api/overview` 增加 `version`；GUI 优先选择版本一致的本机服务，避免复用旧版 daemon
  导致新接口 404。

### 新增（扫码快连）
- `flix qrcode --link` 输出 Android 端唯一能解析的 `relaydesk://pair?fp&ip&port&code`；
  `--with-model` 再把模型网关地址与令牌写进同一张码，一次扫码完成配对 + 模型共享配置
  （网关未开启时只带配对信息并提示先 `flix models start`）。
- 桌面 GUI「新建 / 添加设备」菜单新增「显示连接二维码…」，二维码由
  `GET /api/pair-link` 生成，普通模式下也无需手输 IP 或令牌。
- `flix adb connect`：mDNS（`_adb-tls-connect._tcp`）自动发现无线调试端口 → Apple
  python loopback 中继绕开 macOS 15 本地网络限制 → `adb connect` 并以 `adb devices`
  校验；端口被占用时自动顺延，`--stop-relay` 收尾。中继源码内嵌，冻结包同样可用。
- Android：扫码解析 `model_base` / `model_token`，配对成功后写入偏好并提示已配置网关。

### 新增
- 局域网模型共享网关：`flix models start|status|token`，把手机（Anthropic Messages）
  的请求反向代理到本机 cc-switch（默认 `127.0.0.1:15721`），带固定令牌鉴权、
  SSE 流式透传与 401/502/504 明确失败码。
- `flix serve` / `flixd` 新增 `--model-port`；GUI 设置页新增「模型共享」开关、
  二维码与配置复制；令牌保存在本地 prefs，重启后按开关状态自动恢复。

### 修复 / 优化（模型网关）
- 请求体上限从 aiohttp 默认的 1 MiB 提到 32 MiB（`max_body_mb` 可调），超限返回
  Anthropic 格式的 413，而不是 aiohttp 的纯文本错误页。
- 透传上游的 `request-id`、`retry-after`、`anthropic-ratelimit-*` 响应头；4xx/5xx 计入
  失败，流式中途客户端断开计入 aborted，不再抛栈。
- `/health` 增加 uptime 与请求/成功/失败/拒绝/进行中计数，GUI 设置页与
  `flix models status` 直接展示。
- 设置页可改上游地址（仅 http/https，保存后自动重启网关）；`ipconfig` 结果缓存 60s。

### 安全 / 正确性（对抗审查后修复）
- **配置源统一**：GUI 设置页原先把 `modelGateway` 写进 `~/.relaydesk_gui.json`，而网关读
  `%APPDATA%/flixcli/shared_preferences.json`，导致界面显示的令牌对运行中网关无效（手机 401）。
  现在 GUI 直接使用 daemon 的 Store。
- SSE 已发出响应头后，上游中途断开不再尝试写第二个响应（会破坏 chunked 流），改为关闭连接并计数。
- 模型端口被占用不再拖垮整个 daemon（设备通道/桌面 API 继续运行），提示不再误导到 `--device-port`。
- 停止网关改为「端口 → 进程名」校验后终止，删除持久化 PID（PID 复用可能杀掉无关进程）。
- 查询串令牌仅对使用说明页有效，`/v1/messages` 必须用请求头；`/health` 无令牌只返回 `{"ok":true}`；
  页面加 `no-store`/`no-referrer`，Base URL 不再信任 Host 头。
- 上游地址默认仅允许回环（防 SSRF），拒绝带用户名密码的 URL；远端上游需显式
  `--allow-remote-upstream` / `allow_remote_upstream`。
- 日志不再打印完整令牌；请求体上限限制在 1–128 MiB；端口与配置解析容错。

## [v2.4.0] - 2026-11-09

### 新增
- Windows 11 原生 Acrylic 毛玻璃：由 DWM 实时渲染窗口背后的内容，窗口移动/换背景都正确；
  DWM 不可用时自动回落到原有的截图材质。
- Windows 发布拆成两个包：默认 `win64.zip`（lite，不含媒体栈）与 `win64-full.zip`
  （含 aiortc/av/numpy/PIL，保留屏幕共享）。自动更新默认只取 lite，已安装 full 的机器保持 full 通道。

### 变更
- 打包裁剪：不再收集未使用的 Qt 模块（Qml/Quick/Pdf/虚拟键盘/OpenGL）、软件 OpenGL 回退、
  多余插件与 140+ 语言翻译；默认不打包 media 可选依赖。lite 包从约 109 MB 降到约 42 MB。
- Windows lite/full 包均包含 PTY 支持（`.[gui,pty]`），`shell --pty` 在冻结包中可用。
- `flix ui`（Tk 窗口）退役，调用时返回明确提示并指向 `flix gui`。

### 修复
- 单文件包体积与启动时的临时解压开销（Qt 与 media 依赖不再整包塞入）。
- 毛玻璃材质在窗口移动后裁错区域、显示冻结桌面快照的问题（改用系统材质后不再有自绘层）。

## [v2.3.0] - 2026-11-06

### 安全
- 收紧局域网信任边界：`/bubble`、剪贴板、rdShell、rdAgent、群快照只接受已配对且来源 IP
  与设备记录一致的发送方；未配对/来源不符返回 403 且不落库。
- 配对必须携带对端 4 位配对码（常量时间比较）；`flix pair` / MCP `flix_pair` 在联网前拒绝无码调用。
- Web 上传文件名只取 basename 并校验最终父目录，阻断 `../../` 路径穿越；Web PIN 所有入口共享限流。
- Desktop API 校验 `Host`/`Origin` 与 JSON Content-Type，阻断 CSRF/DNS rebinding。
- 线日志改为显式 `FLIX_WIRE_LOG` 开启，正文哈希化、PIN 打码、5 MB 轮转。

### 修复
- 同一 `APPDATA` 可启动多个 flixd 并争抢端口/偏好文件：新增单实例锁，第二个实例退出码 5。
- 偏好文件跨进程写入丢键；升级脚本硬编码路径、哈希缺失；发现投毒可改写已配对设备 IP
  （现在先 `ping_v2` 校验身份再接受变更）。
- `flix ui` 跨线程使用 SQLite；MCP `flix_pair` 崩溃；CLI/MCP 建群未推送群快照；shell 参数引号丢失。
- 远端执行输出无上限、超时不杀进程树；outbox 重试沿用旧 `created` 导致刚重试即过期。
- TUN/虚拟网卡被当成局域网接口（现在按默认网关与私网前缀选路，跳过 198.18/100.64/169.254/127）。
- 媒体轨道屏幕抓取可能永久阻塞：单帧抓取 3 秒超时后结束轨道。
- MCP/路由器对 Unicode 数字（`²`.isdigit() 为真但 `int()` 会抛）做端口判断导致崩溃；
  统一改为 ASCII 数字校验。
- 依赖安全升级：`cryptography>=50,<51`、`Pillow>=13,<14`（清除 pip-audit 报告的 46 个已知漏洞）。

### 测试
- 新增 100 轮审计驱动 `scripts/audit_rounds.py`（修复回归、CLI/MCP 矩阵、模糊、A/B 基线、工具链）。
- 新增 `tests/test_fuzz_props.py`（Hypothesis：发现报文、二维码、意图回复、升级脚本转义）。

### 新增
- 窗口材质：可选的毛玻璃效果，取桌面快照后模糊，只作用于标题栏、侧栏、状态栏等功能层，
  内容层保持不透明，保证正文对比度；设置页可随时开关，立即生效并在下次启动沿用。
- 动效偏好：跟随系统「显示动画」设置，关闭后分区切换不再淡入，只保留直接切换。
- 高对比度：系统开启高对比度时自动停用毛玻璃并把开关置灰，用户的偏好保留，关闭高对比度后恢复。
- 窄窗口适配：窗口宽度小于 1240 px 时侧栏由 320 px 收窄到 280 px，实体与会话名按实际
  宽度省略，不再依赖构建时的宽度估算。

### 新增（内置自动更新与键盘导航）
- `flix update`：跟 GitHub Release 自动更新 `RelayDesk.exe`。sha256 校验 → 停进程 →
  换文件（旧版另存 `prev-<stamp>`）→ 按原参数重启 → 健康检查，失败自动回滚。
  `--check` 有新版本时退出码 10，`--dry-run` 停在替换前。
- `scripts/autoupdate.py`：无 `flix` 命令的源码检出可直接跑同一套更新引擎（排障用）。
- 内置自动更新：窗口显示 3 秒后检查一次 Release（设置可关），发现新版后台下载并校验
  sha256，顶部常驻横幅提示；点「重启并安装」才替换并重启。不注册计划任务，不阻塞聊天。
- 键盘导航：`Ctrl+1`…`Ctrl+6` 在设备/搜索/群组/传输/ADB/设置之间跳转，`Esc` 从侧页或远程视图
  回到对话（按钮 tooltip 同步标注）。
- 设置页可检查并安装更新：“检查更新 / 立即更新并重启”直接调用内置的 `flix update`
  （sha256 校验 + 健康检查 + 失败回滚），窗口内无需终端；同时显示当前客户端版本。

### 修复（自动更新与界面导航）
- 自动更新会把自己的 `RelayDesk.exe update` 进程列入待杀名单，GUI 点「立即更新」会在换文件前
  把自己终止；现按 PID 排除自身。同时不再重启 `mcp` stdio 子进程（会让编辑器的 MCP 服务器
  变成死连接），只重启 GUI / `serve` / `agent`。
- 任何会话被打开后，2.5s 一次的状态轮询都会把窗口强制切回聊天页，设置/传输/ADB 实际看不清；
  现轮询只刷新消息流，导航权只属于点击与快捷键（`tests/test_gui_navigation.py`）。
- 侧页（传输/设置）左栏原本会清空成一整块空白；现保留会话列表，点一下就能回到对话。
- 侧页顶部仍显示上一段会话的标题，发送/搜索工具条也留在那里可点（会悄悄发到看不见的会话）；
  现侧页显示自己的标题并隐藏这些动作，返回对话时恢复。
- `flix update --dry-run` 原先在下载前就返回（只打一行 planned），与它自己的说明「下载并校验后再停」
  不一致；现真正走完 下载 → sha256 → 解包 → 布局校验 才停下，坏的发行包会在切换前就报错。
- `RelayDesk.spec` 打包配置被删掉了 winpty 收集与 `flixcli.cli/mcp_server/daemon/ptyshell`
  hiddenimports，现恢复并追加 `flixcli.autoupdate`（否则打包后 PTY 壳与子命令可能缺失）。
- 会话内搜索的词会带到下一段会话：换会话后消息被全部过滤，看起来像会话是空的；
  现切换会话即清空搜索（同一个会话内的轮询不会动它）。
- `__version__` 一直停在 `0.1.0`，导致 `flix --version`、`flix self`、MCP `serverInfo` 都报错版本；
  现与 `pyproject.toml` 对齐（`tests/test_version.py` 防止再漂移）。

## [v2.2.1] - 2026-10-30

### 修复
- 打开窗口后终端反复闪烁：GUI 每 2.5s 轮询会触发 `adb devices`，剪贴板同步会拉起 `clip.exe`，
  无窗口主程序派生的控制台子进程会让 Windows 弹出可见终端。现统一加 `CREATE_NO_WINDOW`。
- 剪贴板读取崩溃：64 位句柄被截断成 32 位导致随机 access violation，会静默终止同步循环。

## [v2.2.0] - 2026-10-29

### 变更
- 三个 exe（`RelayDesk.exe` / `flixd.exe` / `flix.exe`）合并为单文件 `RelayDesk.exe`，
  子命令 `serve` / `mcp` 分别对应后台服务与 AI 插件入口。
- 窗口连不上本机服务时自动拉起一次后台服务并在 3.5 秒后重连，不再停在离线页。

## [v2.1.0] - 2026-10-29

### 新增
- 双机 AI 对 AI 通道：`action=rdAgent` 信封复用 8891 与既有配对关系，不新开端口；
  未配对设备投递返回 403 且不落库。
- 三个 MCP 工具：`flix_agent_send` / `flix_agent_inbox`（可长轮询）/ `flix_agent_reply`。
- outbox：对端离线时消息留在本机，守护进程每 30 秒重投，调用立即返回 `queued`。

## [v2.0.0] - 2026-10-29

### 新增
- Windows 桌面端：群管理（改名/加人/移人/新建）、桌面端发起配对、图片与文件气泡内联渲染、
  应用自绘通知卡片、会话复制（`Ctrl+C` / `Ctrl+Shift+C`）。
- Android 平板端：页头与分段控件重排、卡片式列表与空状态、会话页视觉统一。
- 配对后自动补推群快照；修复测试污染真实配置导致收文件静默失败。

[v2.3.0]: https://github.com/XinwangTao/flix-cli/compare/v2.2.1...v2.3.0
[v2.2.1]: https://github.com/XinwangTao/flix-cli/compare/v2.2.0...v2.2.1
[v2.2.0]: https://github.com/XinwangTao/flix-cli/compare/v2.1.0...v2.2.0
[v2.1.0]: https://github.com/XinwangTao/flix-cli/compare/v2.0.0...v2.1.0
[v2.0.0]: https://github.com/XinwangTao/flix-cli/releases/tag/v2.0.0
