# RelayDesk 迭代计划

更新：2026-11-09（第 20 轮复盘）
主线：板子部署链路 / 自动更新 / Web 与桌面体验 / 协议健壮性。一次只做一项，相关测试全绿后提交；
Android 改动必须跑 `F:/projects/_agent-runs/android-deploy.sh <apk> com.relaydesk.app`，安装、启动、
截图三件事都要真实发生并记录版本。

## 下一步（按顺序）

来源：`fix/relaydesk-hardening` 中尚未进主干的修复（已核对的等价移植见文末）。均为防御性校验/
限流，不改状态机与对外契约语义，按 L0 处理。

1. 桌面端氛围背景：工作区加入缓慢漂移的玉青/蓝紫径向光晕（Qt 自绘，静态或低帧率），
   与 Acrylic/玻璃层共存（Inspira 风格，L0）。
2. `daemon.py` 校验 `--device-port`/`--api-port` 1–65535、`--web-port` 0–65535（移植 2d66d4e）。
3. 自动更新：源码包下载上限 64 MiB（移植 6767cb4）。
4. 自动更新：按运行架构选择 release 资产（移植 ec73373；当前只按平台匹配）。
5. 自动更新：systemd 服务重启校验与 `execv` 回退（移植 f3415e9）。
6. 部署脚本：本地/远端私有暂存包 + healthz 轮询 30s（移植 ccf8093）。
7. Web 上传临时文件唯一化：并发同名上传不再共用 `<name>.flixdownload`（L0，非安卓单测）。
8. `EndpointTracker` 定期清理过期空指纹（移植 49c205d；当前只在 remember 时清自己的行）。
9. 板子重复部署清理已删除模块（L1，先补 Archify 图并确认；并行任务已备
   `flix_board_deploy_workflow` 草稿）。
10. Web 同名上传自动改名（L1，先补图确认）。

## 待上机验证

- HyperOS `LcdOff` 冻结：3 分钟关屏 soak 已通过；更长时段与“无限制/允许后台”设置仍需验证。
- 板子（192.168.137.162）与平板（192.168.236/23）不同网段，组播/上行需同一 LAN 或加路由。
- 发布 APK 的 CI debug 签名每次不同：覆盖升级需卸载或统一 release keystore，否则只能卸载重装。
- macOS/Linux 资产的实际替换（`detect_target` 当前只认 Windows exe）。

## 第 20 轮状态

- 本地 `main` 领先 `origin/main`，本地迭代均未推送。
- 测试：386 passed + 131 subtests（e2e 与缺 hypothesis 的 fuzz 除外）。
- 手机：PKJ110 运行 2.4.6 (9)；截图 `device-shot-copy-menu.png`、`device-shot-copy-pasted.png`、
  `device-shot-final.png`（均在 `F:/projects/_agent-runs/`）。第 16–20 轮无 Android 改动，无需重装。
- 已完成：液态玻璃 UI、长按复制、文件 `openFileDescriptor` fallback、8891 通道自愈、关屏保活、
  自动更新解包防穿越/最高版本/per_page 100/404 回退/token/源码版本校验、Web 设备刷新/上传详情/
  群页面错误、`/send` 失败保留输入与 JSON 502、群聊网页同毫秒消息按 id 去重、发现报文
  指纹/端口/显示字段边界校验、Web 上传按接收上限限流、`/healthz` 探测容错、
  桌面端按钮高对比（实色 accent 描边 + 主按钮渐变，边界达非文本 3:1）、
  毛玻璃失焦/轮询稳定性（失焦用自有快照、重写 DWM 深色属性、跳过无变化重建）。
- 视觉参考 Inspira UI（Vue/Nuxt 组件库，无法直接用于 Qt/Android/aiohttp，只移植视觉语言）。

### hardening 已核对等价（无需再移植）

最高版本选择、解包防穿越、上传限制在 saveDir 内、PIN 共享限流、群名文本节点、`/send` JSON 502、
同毫秒去重、per_page 100、坏 payload 跳过、GITHUB_TOKEN、源码版本校验、WS PIN 鉴权、设备列表
刷新、群错误提示、失败保留输入、上传落盘名/转发结果、Android 通道自愈。

### 其他

- `scripts/audit_rounds.py` 在主检出仅有 CRLF 行尾差异，未提交。
- 旧 worktree `ui-liquid-glass`、`merge-v240`、`suspicious-jackson` 均已是 main 祖先，可清理；
  `iter-web-health` 分支（hardening）仍领先 main，带未跟踪的 Archify 板子部署图草稿。
- 未运行 e2e（需 PC + 板子 + 平板同网段）。

