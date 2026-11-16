# 扫码快连与模型共享 Brief

状态：待确认（图：`docs/archify/flix_scan_connect_workflow.html`）
日期：2026-11-16

## 要解决的问题

1. 手机连桌面要分两步：先扫码配对，再手输模型网关地址与令牌，容易输错、换网就失效。
2. macOS 15 的「本地网络」权限会拦掉 adb 直连局域网，现有 `~/tools/adb-wireless.sh` 是手写脚本，没进产品。

## 目标（可验收）

- 手机扫**一张**二维码，完成：配对落库 + 写入模型网关 `base_url` 与 `token`；随后 App 发消息能打到本机 cc-switch（15721）上的模型。
- `flix adb connect` 一条命令：自动发现无线调试端口 → 建立 loopback 中继 → `adb connect` 成功且 `adb devices` 可见。
- GUI「连接设备」按钮能直接显示这张二维码；失败给出可读原因。

## 范围

**做**：桌面端二维码 payload 扩展（`relaydesk://pair`，对齐 Android 现有解析）、GUI/CLI 入口、`flix adb connect` 子命令（mDNS + 中继）、模型网关配置随码下发、图与测试。

**不做**：Mac 摄像头扫手机二维码；读取 `~/.cc-switch/cc-switch.db` 供应商表；多上游分流；改 cc-switch 侧任何配置；ADB 授权/root 相关能力。

## 关键约束

- 模型侧复用现有转发（`/v1/messages` → `127.0.0.1:15721`），不新增密钥通道。
- 配对失败不落库；二维码只携带网关现有令牌，不引入新的长期机密。
- Intel macOS 上 `cryptography` 只能用 48.x（上游 50.x 已停发 x86_64 轮子），本机构建需 `--no-deps` 单独装依赖。

## 待确认

1. 桌面端是否统一改发 `relaydesk://pair?...`（Android 唯一认的格式），`flix2://` 仅保留给 CLI 对端？
2. ADB 中继端口固定 40009 还是按设备自增（多机同时连）？
3. 模型网关未启动时，二维码是拒绝生成，还是只带配对信息？
