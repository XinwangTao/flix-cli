"""验证验证器：给每个自检脚本注入一个已知错误，确认它真的会失败。

如果检查器对「明显的错误」也返回 0，那它守不守都没区别——而一个恒绿的保护网
比没有更糟（给人虚假的安全感）。

做法：备份 -> 注入 -> 跑检查器（期望非 0）-> 恢复。全程在本进程里做文件操作，
不依赖外部状态。失败注入会被立刻撤销，即使中途异常也走 finally。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name:<36} {detail}", flush=True)


def run(script: str) -> tuple[int, str]:
    proc = subprocess.run([PY, str(ROOT / "scripts" / script)],
                          cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, out


def probe(name: str, script: str, target: Path, mutate, restore_content: str) -> None:
    """注入 mutate(原文) -> 跑检查器 -> 期望非 0 -> 恢复。"""
    original = target.read_text(encoding="utf-8")
    try:
        target.write_text(mutate(original), encoding="utf-8")
        code, out = run(script)
        tail = " ".join(out.splitlines()[-1:])[:70] if out else ""
        record(name, code != 0, f"exit={code} {tail}")
    except Exception as exc:  # noqa: BLE001
        record(name, False, f"{type(exc).__name__}: {exc}")
    finally:
        target.write_text(restore_content if restore_content else original,
                          encoding="utf-8")


def main() -> int:
    # 0) 先确认「干净状态下全部为 0」——否则注入测试没有意义
    clean_ok = True
    for script in ("check_doc_paths.py", "check_doc_commands.py",
                   "check_doc_env_vars.py", "check_control_chars.py",
                   "check_missing_imports.py", "check_config_syntax.py"):
        code, out = run(script)
        if code != 0:
            clean_ok = False
            print(f"  基线非 0：{script} -> {out.splitlines()[-1][:80]}")
    record("基线：所有检查器干净时返回 0", clean_ok,
           "6 个检查器" if clean_ok else "见上")

    # 1) 文档路径检查器：引用一个不存在的文件
    doc = ROOT / "docs" / "发版清单-v2.6.6.md"
    probe("注入：文档引用不存在路径", "check_doc_paths.py", doc,
          lambda t: t + "\n\n见 `scripts/definitely_not_here_xyz.py`。\n", "")

    # 2) 文档命令检查器：写一个不存在的子命令
    probe("注入：不存在的 models 子命令", "check_doc_commands.py", doc,
          lambda t: t + "\n\n跑 `flix models nonexistent_command_xyz`。\n", "")

    # 3) 控制字符检查器：塞一个响铃符。
    #    注意注入点要真实存在 —— 我第一次写 "# RelayDesk"，而 README 的标题是
    #    "# flix-cli"，replace 什么都没换，于是检查器「没报错」被我误判成它失灵。
    readme = ROOT / "README.md"
    probe("注入：README 里的控制字符", "check_control_chars.py", readme,
          lambda t: t.replace("# flix-cli", "# flix\x07-cli", 1), "")

    # 4) 缺失 import：删掉一个有 os. 的文件的 import os
    target = ROOT / "tests" / "gateway_multi_client.py"
    probe("注入：去掉 import os", "check_missing_imports.py", target,
          lambda t: t.replace("import os\n", "", 1), "")

    # 5) 配置语法：把 pyproject 的 version 行弄坏
    pyproject = ROOT / "pyproject.toml"
    probe("注入：pyproject 语法错误", "check_config_syntax.py", pyproject,
          lambda t: t.replace('version = "2.6.6"', 'version = 2.6.6 [', 1), "")

    # 6) 恢复后必须回到全绿
    back_ok = True
    for script in ("check_doc_paths.py", "check_doc_commands.py",
                   "check_control_chars.py", "check_missing_imports.py",
                   "check_config_syntax.py"):
        code, out = run(script)
        if code != 0:
            back_ok = False
            print(f"  恢复后仍非 0：{script} -> {out.splitlines()[-1][:80]}")
    record("恢复后重新全绿", back_ok)

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} 通过")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
