"""Markdown 结构检查：表格列数、代码块配对、标题层级跳跃。

为什么值得查：Release 页面就是把这些 md 渲染出来的。表格少一列、代码块没闭合，
GitHub 上会渲染成一坨，而本地用编辑器看往往不明显。

只查结构，不改内容。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "node_modules", ".venv", "build", "dist", "dist_new",
             ".pytest_cache", ".mypy_cache", "android"}


def walk():
    for path in ROOT.rglob("*.md"):
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        yield path


def check_tables(path: Path, text: str) -> list[str]:
    """同一张表里，除分隔行外的列数必须一致。"""
    problems: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("|") and i + 1 < len(lines):
            nxt = lines[i + 1]
            if re.match(r"^\s*\|[\s:|-]+\|\s*$", nxt):
                header_cols = line.count("|")
                j = i + 2
                row = 1
                while j < len(lines) and lines[j].lstrip().startswith("|"):
                    cols = lines[j].count("|")
                    if cols != header_cols:
                        problems.append(
                            f"L{j + 1} 表格列数不一致：表头 {header_cols} 个竖线，"
                            f"本行 {cols} 个")
                    row += 1
                    j += 1
                i = j
                continue
        i += 1
    return problems


def check_fences(path: Path, text: str) -> list[str]:
    """``` 必须成对。"""
    fences = [n for n, line in enumerate(text.splitlines(), 1)
              if line.lstrip().startswith("```")]
    if len(fences) % 2:
        return [f"代码块未闭合：共 {len(fences)} 个 ```（奇数），最后开在 L{fences[-1]}"]
    return []


def check_heading_jumps(path: Path, text: str) -> list[str]:
    """标题层级不跳级（H1 -> H3 会渲染出空洞）。"""
    problems: list[str] = []
    prev = 0
    in_fence = False
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^(#{1,6})\s", line)
        if not match:
            continue
        level = len(match.group(1))
        if prev and level > prev + 1:
            problems.append(f"L{n} 标题从 H{prev} 跳到 H{level}")
        prev = level
    return problems


def main() -> int:
    total = 0
    for path in walk():
        rel = str(path.relative_to(ROOT))
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"  {rel}: 读不出 ({exc})")
            total += 1
            continue
        issues = check_tables(path, text) + check_fences(path, text) + \
            check_heading_jumps(path, text)
        if issues:
            total += len(issues)
            print(f"\n{rel}")
            for item in issues:
                print(f"  - {item}")
    print()
    if total:
        print(f"{total} 处结构问题")
        return 1
    print("Markdown 结构正常（表格列数一致、代码块成对、标题不跳级）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
