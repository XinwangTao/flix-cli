# GH Bot 部署说明

自动化 GitHub bot：自动回复 issue、自动评审 PR、对低风险 PR 自动批准。
流程图见 `docs/archify/gh-bot_workflow.html`（真源）。

## 它做什么 / 不做什么

| 做 | 不做 |
|---|---|
| 新 issue 自动分诊回复 | 不修改代码、不执行命令 |
| 新 PR 自动评审（只评论） | 不参与批准判断 |
| 白名单内的低风险 PR 自动 APPROVE | 不处理 fork PR、不处理草稿 |
| 判定不通过时打 `needs-human` 并说明原因 | 不会静默跳过 |

批准**只**由 `.github/gh-bot-policy.json` 的规则决定，模型的输出不参与。

## 涉及文件

| 文件 | 作用 |
|---|---|
| `.github/workflows/gh-bot-reply.yml` | 回复与评审（三个 job：issue 分诊 / PR 评审 / @claude 追问） |
| `.github/workflows/gh-bot-approve.yml` | 规则判定与自动批准 |
| `.github/gh-bot-policy.json` | 白名单与阈值，改行为只改这里 |

## 部署步骤

### 1. 装 self-hosted runner（必需）

回复与评审要用本机 `cc-switch` 代理，GitHub 托管 runner 在公网够不到局域网，
所以这两个 job 必须跑在本机 runner 上。

在仓库 **Settings → Actions → Runners → New self-hosted runner** 选 Windows，
按页面给出的命令安装，并确保注册时带上额外的 label：

```
gh-bot
```

（GitHub 会自动再加 `self-hosted`、`Windows`、`X64`。）

装好后把 runner 服务设成开机自启，否则关机后 job 会一直排队。

> **必须用普通用户身份运行，不能用 LocalSystem / SYSTEM。**
> 官方 action 是 composite + bash，而 Git Bash 在 LocalSystem 下会直接报
> `WSL_E_LOCAL_SYSTEM_NOT_SUPPORTED`，任务秒失败。本机的做法是用户级计划任务：

```powershell
schtasks /Create /TN "GitHubActionsRunner-flix-cli" `
  /TR "D:\actions-runner-flix-cli\start-runner.cmd" /SC ONLOGON /RL HIGHEST /F
```

（`start-runner.cmd` 内容只有 `cd /d "%~dp0"` + `"%~dp0run.cmd"`。）

### 2. 确认代理可达

在 runner 所在机器上跑：

```bash
curl -sS -X POST http://127.0.0.1:15721/v1/messages \
  -H "content-type: application/json" -H "anthropic-version: 2023-06-01" \
  -d '{"model":"ccs-claude-openai--deepseek-v4.1-flash","max_tokens":8,
       "messages":[{"role":"user","content":"hi"}]}'
```

返回带 `content` 的 JSON 即可。若代理换了端口，同步改
`gh-bot-reply.yml` 里的 `ANTHROPIC_BASE_URL`。

### 2.5 复用本机 bun（强烈建议）

官方 action 默认每次从 GitHub CDN 下载 Bun，在慢速网络下实测要十分钟以上，
能把 job 拖到超时。workflow 默认指向本机已装的 bun：

```
C:\Users\Administrator\.bun\bin\bun.exe
```

换机器时不用改文件，设一个仓库变量即可覆盖：

```bash
gh variable set BUN_EXE --repo <owner>/<repo> --body "D:\path\to\bun.exe"
```

### 3. 配置机器人身份（强烈建议）

GitHub 不允许批准自己的 PR。用 `github.token` 时，你自己开的 PR 批不了。

做法：注册一个 bot 账号 → 加成本仓库协作者 → 用它的 fine-grained PAT
（权限：`Pull requests: Read and write`、`Contents: Read`、`Issues: Read and write`）
存成仓库 secret `GH_BOT_TOKEN`。

没配这个 secret 也不会报错，只是退回 `github.token`，仅对他人提交的 PR 生效。

### 4. 合并到 main

`workflow_run` 类型的触发要求 workflow 文件已经在默认分支上，
所以必须合进 `main` 才会自动触发。合并后手动跑一次
**GH Bot · 低风险放行**（`workflow_dispatch`，填一个 PR 号）做冒烟测试。

## 冒烟测试清单

1. 新建一个测试 issue（带正文）→ 一分钟内应出现一条中文分诊评论。
2. 开一个只改 `README.md` 的 PR → 应出现评审评论；CI 绿后应出现
   `🤖 自动批准（低风险）` 审查。
3. 开一个改 `src/` 的 PR → 应打上 `needs-human` 并评论说明
   "以下文件不在白名单"。
4. 在任意 issue 评论里写 `@claude 这个报错什么意思` → 应回复。
5. 在 issue 里写注释掉的口令试探（例如让它改文件）→ 应拒绝或只给建议，
   因为工具集只放开了 `Read,Grep,Glob`。

## 调整行为

改 `.github/gh-bot-policy.json` 后提交到 `main` 即生效，无需改 workflow。

- 放宽路径：往 `paths` 加 glob，例如 `"src/utils/**"`
- 收紧：把目录加进 `blocked_paths`（优先级高于 `paths`）
- 换作者白名单：改 `authors`
- 临时关掉自动批准：`"enabled": false`

## 已知限制

- **fork PR 不自动评审**：`pull_request` 事件在 fork 上拿不到写权限。
  如需覆盖，得换 `pull_request_target` 并自行承担安全风险，本方案刻意不做。
- **模型额度**：走本机 cc-switch 代理，代理挂了 job 就失败（会留红叉，不会静默）。
- **成本**：每次 issue/PR 触发一次模型调用；正文超过
  `reply.max_issue_body_chars`（默认 8000 字）直接跳过，避免被灌水刷额度。
