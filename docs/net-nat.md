# 给板子开外网（Windows 侧）

板子用 PC 的网线上网：PC 的以太网（192.168.137.1）是板子的网关，WLAN 是 PC 自己的出口。
Windows 的「Internet 连接共享」(ICS) 被热点切换弄丢过，改用 WinNAT + 转发，效果一样且更稳。

## 一次性配置（管理员）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\win_nat.ps1
```

做的事：
1. `Set-NetIPInterface -Forwarding Enabled` 打开「以太网」和「WLAN」的转发；
2. 删掉旧的 NetNat，新建 `boardnat`（`192.168.137.0/24`）；
3. 把结果写进 `nat_report.txt` 便于核对。

配置随系统重启保留（WinNAT 是持久对象，转发开关也写在注册表里）。

## 验证

```bash
# 板子上
ping -c2 223.5.5.5
curl -s -o /dev/null -w '%{http_code}\n' https://pypi.org/simple/   # 期望 200
```

## 兜底：没有 NAT 时用 HTTP 代理

```bash
python scripts/win_http_proxy.py 3128        # PC 侧，普通权限即可
# 板子上
export https_proxy=http://192.168.137.1:3128 http_proxy=http://192.168.137.1:3128
```

只覆盖 HTTP/HTTPS，ping 与任意端口不通；NAT 恢复后记得把代理环境变量删掉，
否则 pip/uv 会一直绕代理。
