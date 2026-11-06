# 开机发 AP + 网页配网

板子接上电源、没有可用网线时，自己发一个热点 `RelayDesk-Setup`（密码 `relaydesk`）。
手机或电脑连上它，打开页面就能选要加入的 Wi-Fi 并填密码；板子切过去后页面会给新地址。
连不上会退回配网模式，不会变成一块连不上的砖。

## 板子侧

```bash
sudo /opt/flixcli/scripts/wifi_ap_setup.sh            # 手动起一次
sudo systemctl enable --now wifi-provision            # 开机自动进配网模式
sudo systemctl disable --now wifi-provision           # 配好网后关掉，改用 board-wifi
```

脚本做四件事：
1. 停掉 `board-wifi.service` / NM 对 wlan0 的占用；
2. 用临时 supplicant 扫一遍周边 Wi-Fi，结果缓存到 `/run/wifi-scan.txt`
   （qcacld 的 `iw scan` 会卡住，scan_results 更稳）；
3. 起 hostapd（AP 模式）+ dnsmasq（只发 DHCP，不抢 53 端口），地址 `10.42.0.1`；
4. 起配网页面（标准库 http server，端口自动挑 8899/8900/8901，80 常被 nginx 占）。

## 手机/电脑侧

1. 连 Wi-Fi `RelayDesk-Setup`，密码 `relaydesk`；
2. 打开 `http://10.42.0.1:8899/`（脚本会打印实际端口）；
3. 点一个网络 → 填密码 → 连接，约 30 秒出结果；
4. 成功后页面显示板子的新 IP，断开热点去访问它。

## 已知边界

- 板载 QCA1023 驱动不支持 802.11w(PMF)，**强制 PMF 的企业 AP 连不上**；
  手机热点、家宽路由、Windows 热点这类不强制 PMF 的都能连。
- AP 与 STA 不能同时用同一个网卡：切过去之后配网热点就没了，失败会自己回来。
