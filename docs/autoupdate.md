# 自动更新

盯 GitHub Release 页面，有新版本就下载、校验、替换、重启，失败自动回滚。

## 软件内置（默认）

RelayDesk 窗口显示 3 秒后自动检查一次 Release，不阻塞首屏。发现新版本后：

1. 后台自动下载并校验 sha256（`--dry-run` 路径），下载期间窗口顶部显示常驻横幅；
2. 校验通过后横幅变为「新版本 vX 已下载并校验，重启 RelayDesk 即可完成安装」；
3. 用户点「重启并安装」才停窗口、换文件并重启；点「稍后」只收起横幅，不取消已下载的包。

设置页有「启动时自动检查更新」开关（默认开）。只检查启动这一次，运行期间不轮询；
需要复查时点设置页的「检查更新」。不再注册 Windows 计划任务，也不需要外部脚本。

## 命令行入口

同一套引擎也保留 CLI 入口，供无窗口环境和排障使用：

```bash
flix update --check        # 只查，有新版退出码 10，没有 0
flix update                # 有新版就装（Windows 主路径）
flix update --dry-run      # 下载 + 校验，停在替换之前
flix update --force        # 已是最新也重装一遍
```

源码检出没有 `flix` 命令时：`python scripts\autoupdate.py --check`。

## 判断逻辑

`flix update` 不靠"版本号比大小"单独决策，顺序是：

1. **资产摘要**：`update_state.json` 里记录的 zip sha256 == 本次 release 资产 digest → 已是最新。
   GitHub API 的 `digest` 字段就是官方发布的 zip 哈希。
2. **标签比对**：本地记录的 tag 与 release tag 一致，或本地版本 ≥ release 版本 → 已是最新
   （不会降级）。
3. 其余情况才安装；本地版本未知时安装一次并记账，之后靠第 1、2 条收敛。

> **实测坑**：v2.2.1 那次 release 的 exe 是版本修复提交之前的构建，`RelayDesk.exe --version`
> 报的是 `0.1.0`。任何只看版本字符串的更新器都会在这个版本上无限重装；这里第 1 条能兜住，
> 并且首次安装后会把真实 tag 写进 state。

## 会重启哪些进程

更新器只停两类**长驻**实例，并按原命令行重启：

- 无参数启动的桌面窗口；
- `serve`（常驻守护）和 `agent`（自动应答）。

`mcp` 进程**不动**：它用客户端给的 stdin/stdout 走 stdio，杀掉再以脱离父进程的方式重启，
编辑器那边只会看到一个死掉的服务器，还会留下孤儿进程。旧文件在 MCP 进程退出前继续可用；
客户端下次拉起 MCP 时自然用上新版本。

一次性命令（`send`/`pair`/`probe`/`devices` 等）以及更新器自身进程（`update`）同样跳过。
换文件时 Windows 允许重命名正在运行的 exe，所以保留这些进程不影响替换。

## 安装步骤

1. 下载 zip（直连 github.com；失败后依次试 `gh-proxy.com`、`ghfast.top`，支持续传）。
2. 校验 zip 的 sha256（与 API digest 比对，`--asset` 自定义资产时同样生效；资产没有 digest 时
   记日志说明"未校验"，不静默放行）。
3. 解包，认出布局：当前 release 是**单个 `RelayDesk.exe`**（v2.2.0 起重打包成单文件），历史上
   有过 `RelayDesk-v<x>-win64/` 目录形式，两种都支持。
4. 预检 `MZ` 头 + 体积下限，防止把 HTML 错误页当 exe 装进去。
5. 停掉正在运行的实例（先 `taskkill` 温和终止，超时再 `/F`）。
6. `RelayDesk.exe` → `RelayDesk.exe.prev-<时间戳>`，再把新文件复制到位（不用原地覆盖：复制
   失败时旧文件还在，回滚就是一次 `rename`）。
7. 用原来的命令行重启（`serve` 的实例还带原来的 `--device-port/--api-port`），并清掉
   `_PYI*`/`_MEI*` 环境变量——否则新进程会复用旧的解包目录。
8. 健康检查：`serve` 实例要求 API 端口重新监听；GUI 实例只要求进程存活。
9. 失败 → 杀掉新进程、把 `prev-<时间戳>` 换回来、按原参数重启旧版，返回
   `error: unhealthy-rollback`。
10. 成功 → 记账、只保留最近 3 个 `prev-*` 备份。

## 文件位置

| 路径 | 作用 |
|---|---|
| `%APPDATA%\flixcli\update_state.json` | 每个 exe 路径一条记录：tag / 资产哈希 / 安装时间 |
| `%APPDATA%\flixcli\update.log` | 追加日志，超过 1 MB 轮转成 `.log.1` |
| `%TEMP%\relaydesk-update\` | 下载缓存（命中校验的 zip 直接复用，不重下 95 MB） |
| `<exe 目录>\RelayDesk.exe.prev-<stamp>` | 回滚备份，保留最近 3 个 |
| `%APPDATA%\flixcli\update.lock` | 单实例锁；崩溃残留超过 45 分钟自动接管 |

`--state-dir` / `--downloads-dir` 可以把前两项挪走（脚本化测试时用得着）。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功（已是最新，或已完成更新） |
| 2 | 用法错误 |
| 3 | 网络/接口失败（无 release、资产不匹配、API 限流） |
| 4 | 安装失败或已回滚（含并发锁占用） |
| 5 | 本地环境问题（找不到 `RelayDesk.exe`） |
| 10 | `--check` 下发现新版本 |

## 排障

**`HTTP 403 / 429`** — GitHub 未认证接口每小时 60 次/IP。设 `GITHUB_TOKEN` 或 `--token`
即可；日志里会给出限流重置时间。定时任务 3 小时一次远低于限额，但同机多个检查脚本会叠加。

**下载卡住** — 先看 `%TEMP%\relaydesk-update` 里的半成品；重跑会续传。直连失败会自动走镜像，
也可以 `--mirror https://your-proxy/`（可重复指定）替换默认镜像列表。

**`另一个更新进程正在运行`** — 上一次运行崩在 `update.lock` 上。等 45 分钟自动过期，或手动删掉
该文件。

**更新后窗口没起来** — 看 `update.log` 的 `unhealthy -> rollback`；旧版已被换回。此时按
`RelayDesk.exe.prev-*` 里的备份排查新版为何绑不上端口（常见：新版本引入原生依赖，需要
`--fresh-env` 语义；或端口被别的进程占用）。

**`--version` 报 0.1.0 但文件是新版** — 见上文"实测坑"，属于已知历史构建问题，不是更新失败。

## 已知边界

- 只跟 `releases/latest`（可 `--pre` 放宽），不跟随 main 分支的每次提交。
- 不做代码签名校验：只校验发布资产的 sha256。攻击面等于 GitHub 账号本身。
- 目前 release 只提供 Windows 资产；macOS/Linux 上 `--check` 会得到 `no-asset` 错误而不是装错包。
- 不会自动下载/替换 `dist\` 之外的开发副本，除非 `--exe` 显式指定。
