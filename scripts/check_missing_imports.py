"""静态检查：用了 os./sys./json. 却没 import 的文件。

真实案例：第 59 轮把硬编码令牌改成 `os.environ.get(...)` 时，漏掉了
`import os`，于是 `tests/gateway_multi_client.py` 一跑就 NameError。
这类错误不跑就发现不了（pytest 不会收集非 test_ 开头的脚本）。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "node_modules", ".venv", "build", "dist", "dist_new",
             ".pytest_cache", ".mypy_cache", "android",
             # reverse/ 是逆向研究用的样本目录，里面故意留了非法语法
             # （scan_any.py 第 4 行是 `import requests?`），不该被当代码检查
             "reverse"}
CHECK = ("os", "sys", "json", "re", "time", "subprocess", "tempfile",
         "urllib", "pathlib", "shutil", "asyncio", "hmac", "hashlib")


def walk():
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        yield path


def imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def main() -> int:
    problems: list[tuple[str, str]] = []
    checked = 0
    for path in walk():
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError) as exc:
            problems.append((str(path.relative_to(ROOT)), f"解析失败: {exc}"))
            continue
        checked += 1
        have = imported_names(tree)
        # 只关心「名字被用到、但没在 import 里」
        used: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                used.add(node.value.id)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                used.add(node.id)
        for module in CHECK:
            if module in used and module not in have:
                problems.append((str(path.relative_to(ROOT)),
                                 f"用了 {module}. 但没有 import {module}"))
    print(f"检查 {checked} 个 .py 文件")
    if problems:
        print(f"\n{len(problems)} 处：")
        for rel, why in problems:
            print(f"  {rel}: {why}")
        return 1
    print("没有「用了模块却没 import」的情况")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
