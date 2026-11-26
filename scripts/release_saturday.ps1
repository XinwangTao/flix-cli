<#
.SYNOPSIS
    Saturday release: merge the feature branch into main (squash), tag, push.

.DESCRIPTION
    Turns the six manual steps in the release checklist (docs/*checklist*.md)
    into one command.

    Default is DRY RUN: it only prints what would happen and checks the
    preconditions. Nothing is merged, tagged or pushed until you pass -Execute.

    Squash mode is deliberate. This repository is public and the feature
    branch's history contains real LAN gateway tokens (9 commits) - see the
    warning section at the top of the checklist). A squash merge gives the
    public repo a
    single commit whose tree is already scrubbed, and no commit in main's
    history ever contains the tokens.

    Kept ASCII-only on purpose: PowerShell 5.1 reads a BOM-less .ps1 as ANSI.

.PARAMETER Execute
    Actually perform the merge, tag and push.

.PARAMETER Version
    Version to tag, e.g. v2.6.6. Defaults to the version in pyproject.toml.

.PARAMETER SkipTests
    Skip the pre-flight test run (not recommended).

.PARAMETER SkipPush
    Do the merge and tag but stop before pushing. Used to rehearse the whole
    flow safely, and by anyone who wants to inspect the merge before shipping.

.EXAMPLE
    .\release_saturday.ps1
    # dry run: show the plan and check preconditions

.EXAMPLE
    .\release_saturday.ps1 -Execute
    # do it for real
#>
[CmdletBinding()]
param(
    [switch]$Execute,
    [string]$Version = "",
    [switch]$SkipTests,
    [switch]$SkipPush
)

$ErrorActionPreference = "Stop"
# Always operate on the repo this script lives in, not on wherever the caller is.
# Worth printing: someone may run it from the primary worktree (which is on main)
# and assume that is what gets released.
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
Write-Host "repo: $repo" -ForegroundColor DarkGray
Write-Host "  (this script always acts on the worktree it lives in)" -ForegroundColor DarkGray

function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }
function Ok($msg) { Write-Host "   OK   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "   WARN $msg" -ForegroundColor Yellow }
function Die($msg) { Write-Host "   FAIL $msg" -ForegroundColor Red; exit 1 }
function Dry($msg) { Write-Host "   DRY  $msg" -ForegroundColor DarkGray }

function Invoke-Git {
    # NOTE: this wrapper cannot take short flags like -a / -m: PowerShell tries to
    # bind them as parameters of this function before they reach git ("A positional
    # parameter cannot be found that accepts argument 'tag'"). Long options like
    # --squash are fine. For anything with a short flag, call git directly.
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$GitArgs)
    $out = & git @GitArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host ($out | Out-String) -ForegroundColor Red
        Die ("git " + ($GitArgs -join " ") + " failed")
    }
    return $out
}

# ---------------------------------------------------------------- preconditions
Step "1/6 preconditions"
$first = (git status --porcelain | Measure-Object -Line).Lines
if ($first -gt 0) { Die "working tree is not clean; commit or stash first" }
Ok "working tree clean"

$branch = (git branch --show-current).Trim()
if ($branch -eq "main") { Die "already on main; switch to the feature branch first" }
Ok "on branch $branch"

$pyproject = Get-Content pyproject.toml -Raw
if ($pyproject -notmatch 'version\s*=\s*"([^"]+)"') { Die "cannot read version from pyproject.toml" }
$projVersion = $Matches[1]
if (-not $Version) { $Version = "v$projVersion" }
if ($Version -notmatch '^v') { $Version = "v$Version" }
Ok "project version $projVersion -> tag $Version"

$tagExists = (git tag --list $Version)
if ($tagExists) { Die "tag $Version already exists" }
Ok "tag $Version does not exist yet"

$ahead = [int](git rev-list --count "main..HEAD")
if ($ahead -le 0) { Die "no commits ahead of main" }
Ok "$ahead commits ahead of main"

if (-not $SkipTests) {
    Step "2/6 unit tests (a few minutes)"
    # The bare `python` on PATH here is Anaconda's and has no flixcli/hypothesis.
    # Resolve a venv interpreter explicitly: env override, worktree .venv, then the
    # primary worktree's .venv, and only then whatever `python` happens to be.
    # NOTE: wrap in @() — a pipeline that yields ONE item returns a bare string,
    # and indexing that gives you the first character ("F"), which then gets run
    # as a command. That is exactly what happened on the first attempt.
    $candidates = @(
        @(
            $env:RELAYDESK_PYTHON,
            (Join-Path $repo ".venv\Scripts\python.exe"),
            (Join-Path (Split-Path -Parent $repo) "flix-cli\.venv\Scripts\python.exe")
        ) | Where-Object { $_ -and (Test-Path $_) }
    )
    if ($candidates.Count -gt 0) {
        $pyExe = [string]$candidates[0]
    } else {
        $pyExe = "python"
        Warn "no venv found; using bare 'python' (may lack flixcli/hypothesis)"
    }
    Ok "tests with $pyExe"
    $env:PYTHONPATH = "src"
    $log = Join-Path $env:TEMP "relaydesk-release-tests.log"
    & $pyExe -m pytest tests -q --ignore=tests/e2e.py --ignore=tests/test_autoupdate_macos.py *> $log
    $code = $LASTEXITCODE
    $tail = Get-Content $log -Tail 12
    Write-Host ($tail | Out-String)
    # These two fail on Windows in pristine main as well; Linux CI is unaffected.
    $knownFailures = @(
        "test_prefs_file_holding_the_token_is_owner_only",
        "test_late_approval_confirms_peer"
    )
    $failedLines = @(Select-String -Path $log -Pattern "^FAILED " | ForEach-Object { $_.Line })
    $unexpected = @($failedLines | Where-Object {
        $line = $_
        -not ($knownFailures | Where-Object { $line -like "*$_*" })
    })
    if ($unexpected.Count -gt 0) {
        Write-Host ($unexpected | Out-String) -ForegroundColor Red
        Die "unexpected test failures (see $log)"
    }
    if ($code -ne 0 -and $failedLines.Count -eq 0) {
        Die "pytest exited $code but reported no FAILED lines (see $log)"
    }
    Ok "tests passed (only the 2 known Windows failures)"
} else {
    Warn "2/6 tests skipped (-SkipTests)"
}

# ---------------------------------------------------------------- tokens in history
Step "3/6 token leak check on the branch history"
# Token values are supplied via environment so this script can live in a
# public repo. Set them before running -Execute, e.g.
#   $env:RD_TOKENS = "rd-aaa...,rd-bbb..."
# If unset, the check still runs but only reports what it can: it greps for the
# local gateway token from %APPDATA%\flixcli prefs (never printed).
$knownTokens = @()
if ($env:RD_TOKENS) {
    $knownTokens = @($env:RD_TOKENS -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}
if ($knownTokens.Count -eq 0) {
    $prefs = Join-Path $env:APPDATA "flixcli\shared_preferences.json"
    if (Test-Path $prefs) {
        try {
            $cfg = Get-Content $prefs -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($cfg.modelGateway -and $cfg.modelGateway.token) {
                $knownTokens = @($cfg.modelGateway.token)
                Ok "using the local gateway token from prefs (value not printed)"
            }
        } catch { }
    }
}
if ($knownTokens.Count -eq 0) {
    Warn "no tokens to check (set RD_TOKENS); skipping the leak scan"
}
$worktreeHits = @()
foreach ($token in $knownTokens) {
    $hit = (git grep -l -e $token 2>$null)
    if ($hit) { $worktreeHits += $hit }
}
if ($knownTokens.Count -gt 0 -and $worktreeHits.Count -gt 0) {
    $worktreeHits | Sort-Object -Unique | ForEach-Object { Write-Host "   $_" }
    Die "the working tree still contains a real token; scrub it first"
} elseif ($knownTokens.Count -gt 0) {
    Ok "working tree has no known tokens"
}

$historyHits = @()
foreach ($token in $knownTokens) {
    $historyHits += @(git log main..HEAD -S $token --oneline)
}
if ($historyHits.Count -gt 0) {
    Warn "$($historyHits.Count) commit(s) in the branch history still contain a token"
    Warn "squash merge keeps them out of main -> this is exactly why we squash"
} else {
    Ok "branch history looks clean too"
}

# ---------------------------------------------------------------- where is main
# git refuses `checkout main` when another worktree already has it. This repo does:
# the primary worktree stays on main while this script runs from a feature
# worktree. So merge *there* instead of checking out here.
$mainWorktree = ""
$wtPath = ""
foreach ($line in (& git worktree list --porcelain 2>&1)) {
    if ($line -like "worktree *") { $wtPath = $line.Substring(9).Trim() }
    elseif ($line -like "branch refs/heads/main") { $mainWorktree = $wtPath }
}
$here = (Get-Location).Path
$normHere = $here -replace "\\", "/"
$useOther = $false
if ($mainWorktree) {
    $normMain = $mainWorktree -replace "\\", "/"
    if ($normMain -ne $normHere) {
        $dirty = (git -C $mainWorktree status --porcelain | Measure-Object -Line).Lines
        if ($dirty -gt 0) { Die "main worktree $mainWorktree is not clean ($dirty files)" }
        $useOther = $true
        Ok "main lives in another worktree: $mainWorktree (clean)"
    }
} else {
    Ok "no worktree holds main yet"
}

# ---------------------------------------------------------------- plan
Step "4/6 plan"
if ($useOther) {
    Write-Host "   1) (merge inside $mainWorktree)"
} else {
    Write-Host "   1) git checkout main"
}
Write-Host "   2) git merge --squash $branch"
Write-Host "   3) git commit -m <message from scripts/release_message.txt>"
Write-Host "   4) git tag -a $Version -m 'RelayDesk $Version'"
Write-Host "   5) git push origin main"
Write-Host "   6) git push origin $Version    # triggers release.yml"

if (-not $Execute) {
    Write-Host ""
    Dry "dry run only - nothing changed"
    Write-Host "   re-run with -Execute to actually release" -ForegroundColor Yellow
    exit 0
}

# ---------------------------------------------------------------- execute
Step "5/6 merge + tag"
$target = $here
if ($useOther) {
    $target = $mainWorktree
    Ok "merging inside $target (main is checked out there)"
} else {
    Invoke-Git checkout main | Out-Null
    Ok "on main"
}
$mergeOut = & git -C $target merge --squash $branch 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host ($mergeOut | Out-String) -ForegroundColor Red
    Die "squash merge failed"
}
Ok "squash-merged $branch"
$msgFile = Join-Path $PSScriptRoot "release_message.txt"
    if (Test-Path $msgFile) {
        $msg = (Get-Content $msgFile -Raw -Encoding UTF8).Trim() -replace "\{VERSION\}", $Version
    } else {
        $msg = "feat: $Version LAN model sharing hardening"
    }
    $msgTmp = Join-Path $env:TEMP "relaydesk-commit-msg.txt"
    [IO.File]::WriteAllText($msgTmp, $msg, (New-Object System.Text.UTF8Encoding($false)))
    $commitOut = & git -C $target commit -F $msgTmp 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host ($commitOut | Out-String) -ForegroundColor Red
        Die "commit failed"
    }
Ok "committed"
$merged = (git -C $target log --oneline -1)
Write-Host "   $merged"
# 必须在 $target（main 所在 worktree）里打 tag。之前漏了 -C，tag 打在了特性
# worktree 的 HEAD 上——树内容虽然一样，但 tag 指向的不是 main 的提交，
# 推出去 Release 就挂在一个游离提交上。
$tagOut = & git -C $target -c user.name="陶鑫旺" `
    -c user.email="taoxinwang@example.com" `
    tag -a $Version -m "RelayDesk $Version" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host ($tagOut | Out-String) -ForegroundColor Red
    Die "tag failed"
}
# 打完立刻核对：tag 必须指向 main 的 HEAD。
$tagCommit = (& git -C $target rev-parse "$Version^{commit}" 2>&1 | Out-String).Trim()
$headCommit = (& git -C $target rev-parse HEAD 2>&1 | Out-String).Trim()
if ($tagCommit -ne $headCommit) {
    Die ("tag $Version points at $tagCommit but main is at $headCommit")
}
Ok "tagged $Version (-> $($headCommit.Substring(0, 7)))"

# Verify the merged tree really carries no token before pushing.
$leak = ""
foreach ($token in $knownTokens) {
    $hit = (git grep -l -e $token 2>$null)
    if ($hit) { $leak += $hit }
}
if ($leak) { Die "merged tree contains a token; not pushing (run: git reset --hard ORIG_HEAD)" }
Ok "merged tree carries no known token"

if ($SkipPush) {
    Write-Host ""
    Warn "6/6 push skipped (-SkipPush)"
    Warn "merged and tagged locally. inspect: git log --oneline -1 ; git show $Version"
    Warn "undo:    git tag -d $Version ; git reset --hard ORIG_HEAD ; git checkout $branch"
    exit 0
}

Step "6/6 push"
$env:GIT_TERMINAL_PROMPT = "0"
Invoke-Git push origin main | Out-Null
Ok "pushed main"
Invoke-Git push origin $Version | Out-Null
Ok "pushed $Version"

Write-Host ""
Write-Host "Done. release.yml is now building 6 platforms." -ForegroundColor Green
Write-Host "  watch:  gh run watch -R XinwangTao/flix-cli" -ForegroundColor Green
Write-Host "  verify: python -m flixcli.cli update --check --json" -ForegroundColor Green

# Releasing means swapping the local exe, which leaves a PyInstaller _MEI* temp dir
# behind (taskkill /F does not let it clean up; ~81 MB each). 19.6 GB had piled up
# across two machines. Clean the stale ones now, while we are here.
$cleaner = Join-Path $PSScriptRoot "clean_mei.ps1"
if (Test-Path $cleaner) {
    Step "housekeeping: stale _MEI temp dirs"
    & $cleaner
}
