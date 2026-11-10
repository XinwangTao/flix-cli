# 启动时检查 GitHub 最新 release（含自动更新）

`flixd` 每次启动都会查一次 `XinwangTao/flix-cli` 的最新 release：

- 结果写日志（`[flixd] 版本检查: 当前 x.y.z 最新 a.b.c（有新版本/已是最新）`）；
- 同时透出到 `GET /healthz` 的 `update` 字段，部署脚本和监控可直接读；
- 设置 `FLIX_AUTO_UPDATE=1`（板子的 systemd 单元里已默认打开）时，发现新版本会
  下载该 tag 的源码包、装进当前 venv、然后 `systemctl restart flixd`；失败只记录，不影响服务。

```json
"update": {"ok": true, "current": "2.3.1", "latest": "2.3.0",
           "available": false, "url": ".../v2.3.0"}
```

## 注意

- 发布包里的 Linux 二进制是 x86_64；aarch64 板子走**源码包**更新（`install_from_source`），
  所以每次更新后 `--alias` 之类的进程参数保持不变，但本地未发布的改动会被 release 覆盖。
- 本地开发分支要把版本号升到高于线上 release（例如 2.3.1），否则会反复“自动更新”回旧版。
- 装依赖优先用 `uv`（systemd 的 PATH 很短，代码里会去 `~/.local/bin/uv` 找），
  找不到就 `ensurepip` 后走 pip。
