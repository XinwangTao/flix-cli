# RelayDesk v2.6.6 — 局域网模型共享加固

> 这一版只做一件事：把「手机用本机 cc-switch 的模型」从能跑变成**可靠、可诊断、可自愈**。
> 全部改动都来自 2026-11-25 的真机联调（Android 手机 + 两台 Windows 工作站）。

## 一句话

手机填一个局域网地址 + 令牌，就能用本机 cc-switch 里的模型；模型名、路由、并发、超时、
诊断全部自动收敛，出问题一条命令说清楚。

## 协议

- **Anthropic Messages**（`POST /v1/messages`）—— 原生支持。
- **OpenAI 兼容**（`POST /v1/chat/completions`）—— 大多数手机 LLM 客户端只支持这个。
  非流式 + SSE 流式（`chat.completion.chunk` + `[DONE]`）、`stream_options.include_usage`、
  **工具调用**（`tools` / `tool_choice` ↔ `tool_use` / `tool_result`，含流式 arguments 分片）。
- **级联中继**：没有 VPN / 没有 cc-switch 的机器可以把网关上游指向有 VPN 的那台，
  由它用它的 VPN 转发；`--upstream-token` 保证第二跳鉴权通过，客户端令牌不下发。

## 修掉的真问题（都有真机证据）

| # | 问题 | 后果 |
|---|---|---|
| 1 | `/v1/models` 没补 `anthropic-version` | cc-switch 返回 220 KB Codex 目录，手机读不出模型 |
| 2 | 短名/旧别名被**静默兜底** | 问 `glm-5.3-flash` 实际回 `dsv41` |
| 3 | 坏路由不熔断 | 手机白等 180 s |
| 4 | 大 body 用总超时 | 33 MB 上传 30 s 报 408 而不是 413 |
| 5 | 自检页给错地址 + 中文网卡名乱码 | 手机复制到死地址 |
| 6 | 流式只回思考内容 | OpenAI 客户端显示空白 |
| 7 | 未知模型名透传 | 被兜底成别的模型 |
| 8 | 错令牌可无限猜 | 已按来源限流（10 次/60 s 锁 30 s） |
| 9 | cc-switch 自动更新窗口 | 偶发 502，已同路由重试一次 |
| 10 | 冷启动 `/health` 显示 0 模型 | 已预热 + 每 90 s 复探 |
| 11 | JSON 不带 charset | PowerShell 客户端中文乱码 |
| 12 | 路由熔断被上游 5xx 喂死 | 全线 503 达 10 分钟 |
| 13 | 模型目录缓存被过滤结果覆盖 | 缓存越用越小，模型永久消失 |
| 14 | `active` 计数漂移 | 卡在 1 不归零 |
| 15 | OpenAI 成功路径没写 `status` | 诊断里 `status=0`，分不清成败 |
| 16 | rdShell 并发只有 4 / PTY 只有 1 | 局域网 SSH 无法并发 |
| 17 | 504 被当成可重试 | 换路由重试 = 重复计费/重复执行（上游可能已受理） |
| 18 | 唯一候选失败后「快失败」60 s | 只剩一条直连路由时，抖一次就让所有设备 503 |
| 19 | 自检页里有从未被读取的 `var MODELS` | 白塞 39 个 id（含死中继）进页面，还和列表口径不一致 |
| 20 | 面板把对端中继说成「不通路由」 | 与「上游正常」自相矛盾，看着像本机坏了 |
| 21 | **级联在 GUI 里配不了** | 填局域网地址只得到「请用 CLI」——核心功能只有 CLI 能用 |
| 22 | 面板**明文**显示网关令牌 | 这个软件本身就是共享屏幕用的，等于把凭据给看的人 |
| 23 | `--watch` 遇网关重启报「新增失败 -2」 | 计数器归零后相减得负数，看不懂 |

## 关键实测数据

- **模型列表自动收敛**：39 个里 28 个属于已死中继 → 自动收敛为 **11 个可用**并保持稳定。
- **网关开销**：配对测量（交替直连/经网关各 4 次）直连 3198 ms vs 经网关 3240 ms →
  **仅 +41 ms**。慢在上游免费额度（1.0–27 s 波动），不是网关。
- **rdShell 并发**：24 路并发 exec 24/24 成功（4.3 s、0 拒绝）；8 路并发 PTY 8/8。
- **功能验收**：`tests/acceptance_lan_models.py` **16 项** —— 本机 **16/16**，
  级联对端 **14/14**（其中「页面与后端一致」需要 CDP 才校验，没有则跳过）。

- **手机页面真机验收**：`tests/cdp_page_acceptance.py` **8 项**全过（页面 JS 执行、
  页面与下拉一致、四种协议×流式组合、工具调用两轮、复制按钮反馈诚实）。
- **长稳**：干净窗口 30 分钟（期间不部署、不跑测试），模型数 11–11、active 峰值 0、
  **新增失败 0、新增拒绝 0**。
- **手机真机**：累计 **70/70** 通过（App 自带 `rdProbe` 深链反复跑「拉模型 + 真实补全」）。
- **多设备并发**：5 客户端 × 2 次 + 手机同时跑 = **10/10 + 手机 1/1**（11.0 s）。
- **级联实证**：对端（无 VPN）经本机出口，`/v1/models` 11 个、两协议都回「通了」；
  本机网关侧确认收到对端请求全部 200。

## 新增命令

```
flix models status     # 一行健康摘要 + 地址（带网卡名）+ 最近请求的路由/耗时
flix models list       # 实时模型短名与完整 id（默认隐藏死路由）
flix models doctor     # 一条命令体检整链 + 手机端可照抄的配置
flix models doctor --watch 600 [--interval 30]   # 持续观测并给漂移结论
```

发布前一条命令验收（16 项：双协议、流式、工具调用、鉴权、并发、断开清理、自检页、
页面与后端一致）：

```
python tests/acceptance_lan_models.py                    # 本机
RD_GATEWAY_TOKEN=<对端令牌> python tests/acceptance_lan_models.py <host> 18993
```

## 新增脚本

- `scripts/setup_lan_model_source.ps1` — 一键把本机变成级联中继（支持 `-DryRun`）。
- `scripts/check_lan_model_source.ps1` — 从任意局域网机器只读验证模型源。
- `scripts/peer_apply_update.ps1` — 在对端换包（**纯 ASCII**、由 schtasks 拉起，
  避免被 RDShell 的作业对象回收；换完自动配好级联）。
- `scripts/peer_install_autostart.ps1` — 开机自启 + 看门狗（每 5 分钟；
  识别「进程在但网关不响应」，连续 2 次才强杀重启）。
- 只读排障：`peer_load.ps1`（CPU/内存画像）、`peer_who.ps1`（高占用进程身份）、
  `peer_ports.ps1`（端口与防火墙）、`peer_edr.ps1`（终端安全软件）、`peer_py.ps1`。

## 怎么配级联（两台机器）

有 VPN 的那台（假设 `.7`，网关 `192.168.1.7:18993`）保持默认上游即可；
没有 VPN 的那台（`.200`）：

1. 桌面端设置页 →「模型共享」→ 勾选
   **「允许上游指向局域网内另一台 RelayDesk（级联中继）」**；
2. 上游填 `http://192.168.1.7:18993`，点「保存上游」；
3. 手机照旧连 `.200` 的地址即可，模型来自 `.7` 的 VPN 出口。

命令行等价：

```
flix models start --upstream http://192.168.1.7:18993 --allow-remote-upstream \
                  --upstream-token <那台机器的网关令牌>
```

> `--upstream-token` 只用于第二跳鉴权，**不会下发给手机**。

## 验收脚本

- `tests/acceptance_lan_models.py` —— 网关协议层 16 项（双协议、流式、工具调用、
  鉴权、并发、断开清理、自检页、页面与后端一致）。
- `tests/cdp_page_acceptance.py` —— 手机页面真机 8 项（走 DevTools 协议读页面状态、
  点按钮）；没有 CDP 时整项跳过。
- `tests/gateway_cascade_soak.py` / `gateway_two_real_devices.py` —— 级联长稳与两真实设备并发。

## 已知限制（写进文档，不藏着）

1. **无 TLS**：令牌在 Wi-Fi 上明文。定位是可信内网工具。
2. **上游并发只有 5**（`claude.jlcops.com` 的 `Limit: 5 concurrent requests, queue: 15`）。
   网关默认 `upstream_concurrency=5` 排队，超出回 429 + `Retry-After`。
3. **上游 60 s 首包墙**：504 是上游 LB（`Server: volcalb`）产生的，**不自动重试**
   （可能已受理，重试=重复计费）。
4. **HyperOS 禁止 adb 注入输入事件**：手机 UI 无法自动化点击，改用 `rdProbe` 深链。
5. **Win11 没有 `wmic`**：查进程用 `Get-CimInstance`。
6. **设备端口随机器而变**：窗口启动 `serve` 时是 `8891 if 8891 空闲 else 18991`。
   所以「这台机器在 8891、那台在 18991」是正常的。`flix shell` 现在会自动再试
   另一个候选端口并提示实际用的那个；`flix shell <ip>`（不写端口）走默认 8891。
7. **`flix send` 不覆盖同名文件**：对端已有同名文件时会生成 `name (1).ext`。
   所以「发新版脚本过去」可能仍是旧文件在跑——用唯一文件名或先删旧的。
8. **给对端用的 `.ps1` 要纯 ASCII**：PowerShell 5.1 把无 BOM 的 `.ps1` 当 ANSI 读，
   中文变乱码、整份解析失败。加 BOM 能救，但容易在某次编辑/传输里丢。

## 升级

自动更新：启动后 `update --check` → 后台 `--dry-run` 下载校验 → 「重启并安装」→
停进程、换 exe、按原参数重启、健康检查，失败回滚。
手动：下载对应包解压覆盖 `RelayDesk.exe`（数据目录 `%APPDATA%\flixcli` 不受影响）。

> 注意：**PyInstaller 产物不能按字节复现**——同一份源码三次构建会有几百字节差异。
> 判断「是不是同一个构建」请看 `--version` 和功能验收，不要比 exe 大小或 hash。
