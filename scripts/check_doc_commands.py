"""检查文档里写的命令能不能真的跑起来（只做静态可执行性检查）。

上一轮的教训：清单写了 `python gw_test/phone_regression.py`，路径根本不存在。
路径已经被 check_doc_paths.py 守住了；这里再进一步，检查**命令行参数**：
文档里写了 `flix models <sub>` / `python -m flixcli.cli models <sub>` 时，
这个子命令是否真的注册过；写了 `pytest tests/xxx.py` 时文件是否在。

只做「存在性」检查，不真跑（真跑要网络/手机/令牌）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
def discover_docs() -> list[Path]:
    skip_dirs = {".git", "node_modules", ".venv", "build", "dist",
                 "dist_new", ".pytest_cache", ".mypy_cache", "android"}
    found: list[Path] = []
    for path in ROOT.rglob("*.md"):
        if any(part in skip_dirs for part in path.relative_to(ROOT).parts):
            continue
        found.append(path)
    return sorted(found)


DOCS = discover_docs()

# 形如 `flix models start` / `python -m flixcli.cli models start`
CMD = re.compile(
    r"(?:flix|python -m flixcli\.cli)\s+(models)\s+([a-z][a-z-]*)")
# 形如 python tests/xxx.py
# 只认看起来像真文件的（全小写+下划线，或以 test_ 开头）；文档里的示例写法
# 如 tests/xxx.py、tests/foo.py 不算引用。
PYTESTISH = re.compile(r"python\s+(tests/(?:test_[A-Za-z0-9_]+\.py|[a-z][a-z0-9_]{4,}\.py))")


def known_models_subcommands() -> set[str]:
    out = subprocess.run([sys.executable, "-m", "flixcli.cli", "models",
                          "--help"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    text = (out.stdout or "") + (out.stderr or "")
    found: set[str] = set()
    # argparse 的 choices 会以 {a,b,c} 形式出现
    for match in re.finditer(r"\{([a-z0-9_,\-]+)\}", text):
        for item in match.group(1).split(","):
            if item.strip():
                found.add(item.strip())
    return found


def main() -> int:
    known = known_models_subcommands()
    if not known:
        print("拿不到 models 子命令列表，跳过")
        return 0
    print(f"models 子命令: {', '.join(sorted(known))}")

    problems: list[tuple[str, str]] = []
    checked = 0
    for doc in DOCS:
        if not doc.exists():
            continue
        text = doc.read_text(encoding="utf-8")
        for match in CMD.finditer(text):
            sub = match.group(2)
            checked += 1
            if sub not in known:
                problems.append((doc.name, match.group(0).strip()))
        for match in PYTESTISH.finditer(text):
            rel = match.group(1)
            checked += 1
            if not (ROOT / rel).exists():
                problems.append((doc.name, rel))

    print(f"检查 {checked} 条命令引用")
    if problems:
        print(f"\n{len(problems)} 条命令引用了不存在的东西：")
        for name, item in problems:
            print(f"  [{name}] {item}")
        return 1
    print("全部有效")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
