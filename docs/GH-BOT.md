# GH Bot 部署说明

自动化 GitHub bot：自动回复 issue、自动评审 PR、对低风险 PR 自动批准。
流程图见 `docs/archify/gh-bot_workflow.html`（真源）。

## 它做什么 / 不做什么

| 做 | 不做 |
|---|---|
| 新 issue 自动分诊回复 | 不修改代码、不执行命令 |
| 新 PR 自动评审（只评论） | 不参与批准判断 |
| 白名单内的低风险 PR 自动 APPROVE | 不处理 fork PR、不处理草稿 |
| 判定不通过时打 `gh-bot-needs-human` 并评论说明原因 | 预期跳过（无关联 PR、PR 已关）不评论 |

批准**只**由 `.github/gh-bot-policy.json` 的规则决定，模型的输出不参与。

## 涉及文件

| 文件 | 作用 |
|---|---|
| `.github/workflows/gh-bot-reply.yml` | 回复与评审（两个 job：issue 分诊 / PR 评审） |
| `.github/workflows/gh-bot-approve.yml` | 规则判定与自动批准 |
| `.github/gh-bot-policy.json` | 白名单与阈值，改行为只改这里 |

## 部署步骤

### 1. 装 self-hosted runner（必需）

回复与评审要用本机 `cc-switch` 代理，GitHub 托管 runner 在公网够不到局域网，
所以这两个 job 必须跑在本机 runner 上（共两个 job：issue 分诊 / PR 评审）。

在仓库 **Settings → Actions → Runners → New self-hosted runner** 选 Windows，
按页面给出的命令安装，并确保注册时带上额外的 label：

```
gh-bot
```

（GitHub 会自动再加 `self-hosted`、`Windows`、`X64`。）

装好后把 runner 设成开机自启，否则关机后 job 会一直排队。本机用的是用户级计划任务：

```powershell
schtasks /Create /TN "GitHubActionsRunner-<repo>" `
  /TR "<runner 安装目录>\start-runner.cmd" /SC ONLOGON /RL HIGHEST /F
```

> **用普通用户身份运行，不要用 LocalSystem / SYSTEM。**
> LocalSystem 下 Git Bash 直接报 `WSL_E_LOCAL_SYSTEM_NOT_SUPPORTED`，
> 任何 `shell: bash` 的步骤都会秒失败。

> **若步骤里用到 bash，还要让 runner 找到 Git 自带的 bash。**
> 常见 PATH 里只有 `<Git 安装目录>\cmd`（有 git.exe、没有 bash.exe），
> `bash` 会解析到 `C:\Windows\System32\bash.exe`——那是 WSL 启动器。
> 启动脚本里先前置 Git 路径：

```bat
@echo off
cd /d "%~dp0"
set "PATH=<Git 安装目录>\bin;<Git 安装目录>\usr\bin;%PATH%"
"%~dp0run.cmd"
```

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

### 2.5 指定 Claude Code CLI 路径

回复与评审直接调用本机已装的 `claude` 可执行文件，不走官方 composite action
（它依赖 bun + bash，在 Windows runner 上踩坑多）。路径由**仓库变量** `CLAUDE_EXE` 提供：

```bash
# 先看全局 npm 根目录，再拼出 exe 路径
npm root -g
# 例如 Windows：<npm 根目录>\@anthropic-ai\claude-code\bin\claude.exe

# 写成仓库变量（不是 secret，只是路径）
gh variable set CLAUDE_EXE --repo <owner>/<repo> \
  --body '<npm 根目录>\@anthropic-ai\claude-code\bin\claude.exe'
```

没配这个变量时，workflow 会直接报「未设置仓库变量 CLAUDE_EXE」。

### 3. 配置机器人身份（想做真自动批准才需要）

GitHub 不允许批准自己的 PR，而且 `github.token` 被明确禁止批准 PR
（实测 422：`GitHub Actions is not permitted to approve pull requests`）。
所以自动批准需要一个**独立身份**。两种做法：

#### 方案 A：GitHub App（推荐，权限最小）

跑一次创建脚本，浏览器点两下即可：

```powershell
python D:\GitHubBot\setup_bot_app.py --repo <owner>/<repo>
```

1. 弹出的页面点 **Create GitHub App**；
2. 按打印的链接把 App **安装到本仓库**；
3. 写 secret：

```powershell
powershell D:\GitHubBot\set_bot_secrets.ps1 -Repo <owner>/<repo>
```

会写入 `BOT_APP_ID` 和 `BOT_APP_PRIVATE_KEY`。workflow 检测到它们就自动用
App 安装 token 提交批准；App 权限只有 `contents: read`、`issues: write`、
`pull_requests: write`。

#### 方案 B：独立 bot 账号 + PAT

注册一个小号 → 加成本仓库协作者 → 用它的 fine-grained PAT
（`Pull requests: Read and write`、`Contents: Read`、`Issues: Read and write`）
存成仓库 secret `GH_BOT_TOKEN`。

**两种都没配也不会报错**：放行流程会退化成「已满足自动批准条件，待人工确认」的评论。

### 4. 合并到 main

`workflow_run` 类型的触发要求 workflow 文件已经在默认分支上，
所以必须合进 `main` 才会自动触发。合并后手动跑一次
**GH Bot · 低风险放行**（`workflow_dispatch`，填一个 PR 号）做冒烟测试。

### 5. 分支保护

只建议开启 **必需状态检查**（`test (3.11)`、`test (3.12)`）。

> [!warning] 不要开「必需 1 个批准」
> 实测（2026-11-09，PR #25）：GitHub App `flix-cli-gh-bot[bot]` 已提交
> `APPROVED`，合并状态仍是 `BLOCKED / REVIEW_REQUIRED`。
> **GitHub 不把机器人/App 的批准计入「必需批准数」**。
> 而你自己又不能批准自己的 PR，所以一旦开启这条，你的 PR 会被永久卡住，
> 只能走管理员旁路合并。
>
> 机器人批准在这里的作用是**信号**（明确的 APPROVED + 说明），不是闸门。
> 真正的闸门是 `.github/gh-bot-policy.json` 里的白名单判定。
>
> 如果确实要人工批准这道门，需要**人类**作为协作者去点 Approve。

```bash
gh api -X PUT repos/<owner>/<repo>/branches/main/protection --input - <<'JSON'
{
  "required_status_checks": {"strict": false, "contexts": ["test (3.11)", "test (3.12)"]},
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
```

## 冒烟测试清单

1. 新建一个测试 issue（带正文）→ 一分钟内应出现一条中文分诊评论。
2. 开一个只改 `README.md` 的 PR → 应出现评审评论；CI 绿后应出现
   `### 🤖 已自动合并` 评论，PR 状态变为 merged。
3. 开一个改 `src/` 的 PR → 应打上 `gh-bot-needs-human` 并评论说明
   "命中禁止路径" 或 "不在白名单"。
4. 把 `README.md` 换成指向 `.git/config` 的符号链接开 PR → 应被拦（"是符号链接"）。

## 调整行为

改 `.github/gh-bot-policy.json` 后提交到 `main` 即生效，无需改 workflow。

- 放宽路径：往 `paths` 加 glob，例如 `"src/utils/**"`
- 收紧：把目录加进 `blocked_paths`（优先级高于 `paths`）
- 换作者白名单：改 `authors`
- 临时关掉自动批准：`"enabled": false`

## 安全边界（2026-11-09 对抗性审查后重做）

模型进程以 runner 用户身份运行，**实测 `Read` 能读整台机器**（`C:\Windows\win.ini`、
`bot-app.pem` 都能读出来）。而 issue/评论正文完全由外部人控制。因此采用「能力跟着信任走」：

| 来源 | 文件工具 | 说明 |
|---|---|---|
| 仓库协作者（OWNER/MEMBER/COLLABORATOR） | `--tools Read`（只有 Read） | 可读仓库文件，质量更高 |
| 其他人 | `--tools ""`（一个工具都不给） | 只能基于正文回答，读不到任何文件 |

所有调用都额外加 `--restricted --strict-mcp-config --bare`：
文件工具被**限制在工作目录内**（实测读不到 `C:\Windows\win.ini`）、忽略仓库自带的
`.claude/settings.json`、不加载 MCP；`--bare` 还会跳过 CLAUDE.md 自动发现（实测：
PR 里塞一个 `CLAUDE.md` 会让内容进入评审上下文，加 `--bare` 后不会）。

**外部来源的分诊在空目录里执行**（不进仓库工作区），即使工具名单漂移也读不到仓库内容；
checkout 一律 `persist-credentials: false`，避免 job token 落进 `.git/config`。

> `--restricted` 与 `--bare` 共同挡住「本机记忆被原文吐出」：不加它们时，一句提示词就能让模型
> 逐字复述 `~/.claude/CLAUDE.md`。**不要为了排查问题删掉这两个开关。**

**按天成本上限**（`reply.max_runs_per_day`，默认 120）只约束**外部来源**的 issue 分诊：
既防刷额度，又不会把维护者自己挡在门外（协作者与 PR 评审不计入）。
权限策略一律从 `main` 读取，取不到就失败，不回落到检出目录。

判定侧（`gh-bot-approve.yml`）的硬规则：

- **删除和重命名同样过禁止路径**：删 `src/x.py`、把 `src/x.py` 移到 `docs/` 都会被拦。
- 新增/删除行数都有上限（`max_additions` / `max_deletions`）。
- 没有任何文件命中白名单（含空 PR）不批准；**删除**也要命中白名单，删掉 `assets/` 这类白名单外文件会被拦。
- **符号链接（120000）和子模块（160000）一律不批**：软链能把 runner 上的文件（例如 `.git/config`
  里的 token）带进构建产物（`pyproject` 的 readme、spec 的 copy_metadata）。
- 文件名含控制字符（含换行）不批；目标分支必须是默认分支；**最后提交作者**也要在白名单内。
- `docs/archify/**` 不参与自动放行：流程图是流程真源，按 Archify 协议需人工确认。
- policy 一律从**默认分支**读（`workflow_dispatch` 可选任意 ref，不能信检出目录）；缺必需字段直接失败。
- **CI 恒为必需**（没有开关）；手动触发会按 `head_sha` 反查 CI 结论（用 workflow 自带 token，App/PAT 无 `actions: read`）。
- 转人工标签不存在时 workflow 会自动创建，不会静默丢掉信号。
- 批准提交带 `commit_id`，且提交前重新确认 head 未变（防判定后被推送）。
- 自动拦截标签是 `gh-bot-needs-human`，与人工阻断标签 `needs-human` 分开，避免自动打标导致永久自锁。

残留风险：协作者账号被接管时仍可利用其权限；如需更强隔离，应给 runner 单独低权限账号
并把私钥移出该账号可读范围。

## 已知限制

- **fork PR 不自动评审**：`pull_request` 事件在 fork 上拿不到写权限。
  如需覆盖，得换 `pull_request_target` 并自行承担安全风险，本方案刻意不做。
- **模型额度**：走本机 cc-switch 代理，代理挂了 job 就失败（会留红叉，不会静默）。
- **成本**：每次 issue/PR 触发一次模型调用；正文超过
  `reply.max_issue_body_chars`（默认 8000 字）直接跳过，避免被灌水刷额度。
  该上限只作用于 issue 正文；PR diff 硬编码 30000 字截断。
