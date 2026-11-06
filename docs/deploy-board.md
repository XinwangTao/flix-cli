# 部署到 Linux 开发板（移远 QCM6490 / QuecPi Alpha）

板子：`qcm6490-idp`，aarch64，Qualcomm Linux 1.3（glibc 2.35，系统 Python 3.10）。
项目要求 Python ≥3.11，所以用 `uv` 自带一份 3.12，不动系统 Python。

## 一条命令

```bash
scripts/deploy_board.sh                 # 默认 quecpi-alpha:/opt/flixcli，web 8892
scripts/deploy_board.sh user@host /opt/flixcli 8892
```

脚本做的事：打包源码 → scp → 首次装 uv/Python 3.12/venv → `uv pip install -e` →
把 `deploy/flixd.service` 模板（替换安装目录与端口）装到 `/etc/systemd/system/` → 起服务 → 用 `/healthz` 自检。

## 验证

```bash
curl http://<板子IP>:8892/healthz
# {"ok": true, "alias": "qcm6490-idp", "channelPort": 8891, "uptime": 12, "devices": 1}
```

- 网页版：`http://<板子IP>:8892/`，启动日志里有 4 位 PIN（`journalctl -u flixd | grep PIN`）。
- 设备通道：8891（发现/收发），桌面 API：127.0.0.1:8898（只给本机 RelayDesk 用）。
- 从 PC 侧验证：`flix discover` 能看到板子；`flix ping <IP>` 握手；`flix send-text <IP> hi` 回 `delivered`。

## 配对

文本不需要配对；文件/剪贴板/命令要求发送方已配对（否则 HTTP 403 `sender not paired`）。
无头板上没有 GUI 可点确认，直接在板上登记对方指纹：

```bash
cd /opt/flixcli && .venv/bin/python -c "
from flixcli.config import Store
s=Store(); s.add_pair('<对方指纹>', 'board-local'); print(s.is_paired('<对方指纹>')); s.close()"
```

## 已知限制

- **板上 Wi-Fi 上不了公司 AP**：AP 的 RSN 里 MFPR=1（强制 802.11w PMF），
  而这块 QCA1023 的 qcacld 驱动不提供 MFP 能力（`iw phy` 无 MFP、`mac_addr=1` 也不被支持），
  于是关联+四次握手能成，几秒后被 AP 以 reason=148 踢掉。
  换不需要 PMF 的热点（手机/Windows 热点）可以正常关联并拿到地址，说明是 AP 策略 + 驱动能力缺口，
  不是配置错误。想彻底无线化：插 USB 网卡（Type-C 口现在是 host 角色，能枚举）或换支持 PMF 的模块。
- 板子以太网走 PC 的 ICS（192.168.137.162），mDNS 名 `qcm6490-idp.local` 可用。
