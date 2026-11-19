# 变更记录

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；版本号与 GitHub Release 标签一致。
可执行产物见 [Releases](https://github.com/XinwangTao/flix-cli/releases)。

## [未发布]

### 修复
- 组播网卡不再被虚拟适配器抢走：Wi-Fi 一闪断，`default_iface_ip()` 就会挑中
  VMware 的 VMnet1（192.168.73.1/24，无网关）或 Hyper-V 的 vEthernet，announce
  全打进没有人听的虚拟链路，本机在手机设备列表里凭空消失，从手机发起的配对自然
  配不上。现在按 GetAdaptersAddresses 的 FriendlyName/Description 过滤
  VMware/VirtualBox/Hyper-V/Wi-Fi Direct/Bluetooth/Wintun/VPN/Clash 一类适配器，
  并把已选网卡钉住（只有它真的消失才换），避免每次轮询都重新猜。
- `flix netcheck` 会打印被跳过的虚拟适配器（地址 + 适配器名），多网卡机器上不用
  再靠猜解释"为什么选了这个 IP"；`shared_preferences.json` 的 `multicast_if`
  可手工钉死网卡，GUI 启动的 daemon 也能生效。
- 桌面端配对超时从 6s 放宽到 32s（`PAIR_TIMEOUT`）：对端可能不在屏幕前，需要等
  它的通知提示，6s 会让一次本来能成功的配对看起来失败。
- Android 设备身份不再随机重铸：指纹缺失时由 ANDROID_ID 派生（同一签名 + 同一
  设备恒定），并用 `commit()` 落盘。清数据/重装后手机不再是"新设备"，旧配对、
  会话线程和设备列表不会再对不上（同一 IP 出现 5 个 PKJ110 指纹就是这个原因）。
- Android 后台也能配对：没有可见 Activity 时不再立刻回 `301 pair Device Null`，
  改为弹高优先级通知（接受/拒绝按钮，25s 有效），把请求挡在 HTTP 线程里等用户点。

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
