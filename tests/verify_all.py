"""全套验收：一条命令跑完模型网关 + 页面真机 + rdShell 并发。

三个入口原本要分开跑，各自需要不同的前置条件（令牌、CDP 转发、测试守护进程）。
这个脚本按顺序跑、汇总结果、给出"能不能发版"的结论。

用法：
    python tests/verify_all.py                 # 本机全部
    python tests/verify_all.py --skip-ui       # 跳过手机页面（没有 CDP 时用）
    python tests/verify_all.py --skip-shell    # 跳过 rdShell 并发

环境变量：
    RD_GATEWAY_TOKEN   本机网关令牌（模型验收必需）
    RD_PEER_URL        级联对端地址，给了就多跑一轮对端验收
    RD_PEER_TOKEN      对端令牌
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = sys.executable

STEPS: list[tuple[str, list[str], dict]] = []


def step(name: str, args: list[str], env: dict | None = None) -> None:
    STEPS.append((name, args, env or {}))


def run(name: str, args: list[str], extra_env: dict) -> tuple[bool, str, float]:
    env = {**os.environ, **extra_env}
    began = time.time()
    # .ps1 files are run by PowerShell, everything else by the current Python.
    target = args[0]
    if target.lower().endswith(".ps1"):
        command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                   "-File", target, *args[1:]]
    else:
        command = [PY, *args]
    proc = subprocess.run(command, cwd=ROOT, env=env,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    elapsed = time.time() - began
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    tail = "\n".join(output.splitlines()[-6:])
    return proc.returncode == 0, tail, elapsed


def main() -> int:
    skip_ui = "--skip-ui" in sys.argv
    skip_shell = "--skip-shell" in sys.argv

    token = os.environ.get("RD_GATEWAY_TOKEN", "")
    if not token:
        print("请先设置 RD_GATEWAY_TOKEN（桌面端「模型共享」面板可复制）")
        return 2

    env_local = {"RD_GATEWAY_TOKEN": token}
    # 先查文档引用的路径/命令是否真实存在 —— 周六照抄清单时最怕这个
    step("文档引用自检", ["scripts/check_doc_paths.py"], {})
    step("文档命令自检", ["scripts/check_doc_commands.py"], env_local)
    step("文档环境变量自检", ["scripts/check_doc_env_vars.py"], env_local)
    step("控制字符自检", ["scripts/check_control_chars.py"], env_local)
    step("Markdown 结构自检", ["scripts/check_markdown.py"], env_local)
    step("缺失 import 自检", ["scripts/check_missing_imports.py"], env_local)
    step("路径转义自检", ["scripts/check_path_escapes.py"], env_local)
    step("PowerShell 语法自检", ["scripts/check_ps1_syntax.ps1"], env_local)
    step("配置文件语法自检", ["scripts/check_config_syntax.py"], env_local)
    # 元检查：给每个检查器注入已知错误，确认它真的会失败（恒绿的网比没有更糟）
    step("检查器有效性", ["scripts/check_checkers.py"], env_local)
    step("exe 与源码新鲜度", ["scripts/check_dist_fresh.py"], env_local)
    step("APK 与 Kotlin 新鲜度", ["scripts/check_apk_fresh.py"], env_local)
    step("模型验收（本机 16 项）", ["tests/acceptance_lan_models.py"], env_local)
    step("README 声明核对", ["tests/check_readme_claims.py"], env_local)

    peer_url = os.environ.get("RD_PEER_URL", "")
    peer_token = os.environ.get("RD_PEER_TOKEN", "")
    if peer_url and peer_token:
        host = peer_url.replace("http://", "").replace("https://", "")
        base, _, port = host.partition(":")
        step("模型验收（级联对端）",
             ["tests/acceptance_lan_models.py", base, port or "18993"],
             {"RD_GATEWAY_TOKEN": peer_token})

    if not skip_ui:
        step("手机页面真机（8 项）", ["tests/cdp_page_acceptance.py"], {})
    if not skip_shell:
        step("rdShell 并发", ["tests/rd_shell_load.py", "--exec", "24",
                            "--pty", "6"], {})

    results = []
    for name, args, extra_env in STEPS:
        print(f"\n=== {name} ===", flush=True)
        ok, tail, elapsed = run(name, args, extra_env)
        print(tail)
        print(f"-> {'PASS' if ok else 'FAIL'}（{elapsed:.1f}s）", flush=True)
        results.append((name, ok, elapsed))

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, elapsed in results:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name:<24} {elapsed:>6.1f}s")
    total = sum(elapsed for _, _, elapsed in results)
    print(f"\n{passed}/{len(results)} 通过，共 {total:.0f}s")
    if passed == len(results):
        print("结论: 可以发版")
        return 0
    print("结论: 有失败项，先修")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
