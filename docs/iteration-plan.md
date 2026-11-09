# RelayDesk 迭代计划

更新：2026-11-09（第 15 轮复盘）
主线：板子部署链路 / 自动更新 / Web 与桌面体验 / 协议健壮性。一次只做一项，相关测试全绿后提交；
Android 改动必须跑 `F:/projects/_agent-runs/android-deploy.sh <apk> com.relaydesk.app`，安装、启动、
截图三件事都要真实发生并记录版本。

## 下一步（按顺序）

1. Web 修复移植（非安卓，单测）：WebSocket 携带 PIN、设备下拉定时刷新、上传结果详情、
   群页面加载/发送错误提示；需适配 v2.4.0 的安全版 webapp。
2. 板子重复部署清理已删除模块（L1，先补 Archify 图并确认；并行任务已备
   `flix_board_deploy_workflow` 草稿）。
3. Web 同名上传自动改名（L1，先补图确认）。

## 待上机验证

- HyperOS `LcdOff` 冻结：3 分钟关屏 soak 已通过；更长时段与“无限制/允许后台”设置仍需验证。
- 板子（192.168.137.162）与平板（192.168.236/23）不同网段，组播/上行需同一 LAN 或加路由。
- 发布 APK 的 CI debug 签名每次不同：覆盖升级需卸载或统一 release keystore，否则只能卸载重装。
- macOS/Linux 资产的实际替换（`detect_target` 当前只认 Windows exe）。

## 第 15 轮状态

- 本地 `main` 0622b04：v2.4.0（lite/full + Acrylic）合并、液态玻璃 UI、Android 文件
  `openFileDescriptor` fallback/8891 通道自愈/关屏保活/长按复制、自动更新解包防穿越与
  最高版本选择、per_page 100/404 回退/token/源码版本校验。
- 测试：340 passed + 111 subtests（e2e 与缺 hypothesis 的 fuzz 除外）。
- 手机：PKJ110 运行 2.4.6 (9)；截图 `device-shot-copy-menu.png`、`device-shot-copy-pasted.png`、
  `device-shot-final.png`（均在 `F:/projects/_agent-runs/`）。
- 未合并：`fix/relaydesk-hardening`（a37875c）仍带剩余 Web/自动更新修复；主检出
  `scripts/audit_rounds.py` 仅 CRLF 行尾差异，未提交。
