<#
    分离启动器：以独立进程运行 peer_apply_update.ps1。

    为什么需要它：RDShell 的执行器（cmd/powershell）是 RelayDesk daemon 的子进程，
    换包脚本要杀掉 RelayDesk，同步调用会把执行器一起杀掉，脚本就断在半路。
    这里用 Start-Process 起一个独立进程，启动器本身立刻返回。
#>
param(
    [Parameter(Mandatory = $true)][string]$Token
)

$target = Join-Path $PSScriptRoot "peer_apply_update.ps1"
if (-not (Test-Path -LiteralPath $target)) {
    Write-Output "ERROR: 找不到 $target"
    exit 2
}
Start-Process -FilePath "powershell" -WindowStyle Hidden -ArgumentList @(
    "-NoProfile", "-ExecutionPolicy", "Bypass",
    "-File", $target, "-ModelSourceToken", $Token
)
Write-Output "launched"
