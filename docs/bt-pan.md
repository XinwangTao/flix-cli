# 蓝牙连接（PAN）同一个板子

目标：没有网线、也连不上 AP 的时候，PC 仍能通过蓝牙登进板子，用 SSH / RelayDesk 的文件和消息通道。

## 结论先说

蓝牙走 **PAN（BNEP，蓝牙上的以太网）** 而不是 RFCOMM：PAN 出来是一个 IP 网卡，
SSH、RelayDesk 的 8891/8892 端口、mDNS 全部原样可用，不需要给 RelayDesk 加新传输层。

## 本机板子的现状（移远 QCM6490 / QuecPi Alpha）

- `bluetooth`、`bnep` 模块都在，`/dev/ttyHS1`、`btattach`、`hciattach` 都有；
- 固件在 `/lib/firmware/qca/tfbtfw11.tlv` 与 `tfbtnv11.bin`；
- 但把 ttyHS1/ttyMSM0 挂上 HCI 后，控制器不回任何命令
  （`hci0: Opcode 0x0c03 failed: -110`，`hciattach` 报 `Fail to get Rome Version`），
  BD_ADDR 全 0、Features 全 0 —— 说明芯片没真正上电/没跑起来，属于板级 BT 供电或固件问题，
  不是配对姿势的问题。

**因此：想要蓝牙通道，先给板子插一个 USB 蓝牙适配器**（Type-C 口现在是 host 角色，能枚举），
插上后 `hci0` 会正常应答，下面的脚本就能直接用。

## 板子侧

```bash
sudo scripts/bt_pan.sh            # 默认 10.55.0.1，NAP 名 RelayDesk-BT
```

脚本会：起 bluetoothd → 打开可发现/可配对 → 起 PAN(NAP) 服务 → 给 bnep0 配地址 →
用 dnsmasq 给对端发 10.55.0.10-50 的地址。

## PC 侧（Windows）

1. 设置 → 蓝牙 → 添加设备 → 选 `RelayDesk-BT`，配对码确认；
2. 控制面板 → 设备和打印机 → 右键板子 → 连接 → 接入点（Access point）；
3. `ipconfig` 里会多一个蓝牙网络连接，地址 10.55.0.x；
4. 之后 `ssh root@10.55.0.1`、`http://10.55.0.1:8892/`、`flix ping 10.55.0.1` 都照旧。

## 和 RelayDesk 的关系

- 不需要改协议：PAN 只是多一张网卡。flixd 的 announce 循环每 30s 会核对出口网卡，
  网线拔掉、切到蓝牙后会自动重新加入组播（`refresh_iface`）。
- 想把蓝牙当首选链路时，启动加 `--multicast-if 10.55.0.1` 即可。

## 已知限制

- 蓝牙 PAN 带宽约 1-2 Mbps，适合 SSH/文本/小文件，不适合大文件传输；
- Windows 的 PAN 客户端偶尔需要断开重连才拿到 DHCP 地址；
- 板载 QCA 蓝牙目前不可用，必须先解决供电/固件或改用 USB 适配器。
