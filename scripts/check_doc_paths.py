"""扫描文档里引用的**仓库内路径**，确认它们真的存在。

上一轮发现清单引用了 `gw_test/phone_regression.py`——仓库里根本没有这个路径，
周六照抄必失败。这类问题用眼睛读很难发现（会跳过），用脚本一扫就出来。

做法：从指定文档里正则提取看起来像路径的 token，只保留「仓库内」的候选：
  - tests/... scripts/... src/... docs/... .github/...
  - 以及 .md/.py/.ps1/.yml 结尾且不含空格的
然后逐个 os.path.exists 检查。外部 URL、绝对路径、通配符跳过。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def discover_docs() -> list[Path]:
    """仓库里所有会被人照着做的 md（含 docs/ 与 docs/product/）。"""
    skip_dirs = {".git", "node_modules", ".venv", "build", "dist",
                 "dist_new", ".pytest_cache", ".mypy_cache", "android"}
    found: list[Path] = []
    for path in ROOT.rglob("*.md"):
        if any(part in skip_dirs for part in path.relative_to(ROOT).parts):
            continue
        found.append(path)
    return sorted(found)


DOCS = discover_docs()

PREFIXES = ("tests/", "scripts/", "src/", "docs/", ".github/", "android/")
SUFFIXES = (".md", ".py", ".ps1", ".yml", ".yaml", ".toml", ".json", ".kt",
            ".spec", ".cmd", ".sh")
# 反引号里常出现命令片段，单独处理
TOKEN = re.compile(r"[A-Za-z0-9_./\\-]+\.[A-Za-z0-9]{1,5}")

SKIP_HINTS = ("<", ">", "...", "*", "$", "%", "|", "http", "~/", ":\\",
              "AppData", "Users", "C:\\", "D:\\", "F:\\", "example",
              "your", "x.y",
              # 文档里常见的「示例路径」写法，不是真引用
              "x.py", "foo.py", "bar.py", "a.py", "b.py", "your_file",
              "/x/", "/y/")


def looks_like_repo_path(token: str) -> bool:
    if not token.endswith(SUFFIXES):
        return False
    if any(hint in token for hint in SKIP_HINTS):
        return False
    if " " in token:
        return False
    return token.startswith(PREFIXES)


def main() -> int:
    problems: list[tuple[str, str]] = []
    checked = 0
    for doc in DOCS:
        if not doc.exists():
            print(f"[skip] {doc.name} 不存在")
            continue
        text = doc.read_text(encoding="utf-8")
        seen: set[str] = set()
        for match in TOKEN.finditer(text):
            token = match.group(0).strip("./")
            if token in seen or not looks_like_repo_path(token):
                continue
            seen.add(token)
            checked += 1
            if not (ROOT / token).exists():
                problems.append((doc.name, token))
    print(f"检查 {len(DOCS)} 份文档，{checked} 个仓库内路径引用")
    if problems:
        print(f"\n{len(problems)} 处引用了不存在的路径：")
        for name, token in problems:
            print(f"  [{name}] {token}")
        return 1
    print("全部存在")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
