"""查 .py 里「Windows 路径写成非 raw 字符串」——覆盖全部转义，不只 \\f。

上一版只守了 \\f/\\v/\\a/\\b/\\0，但真正容易踩的是：
    F:\\temp       ->  \\t 是 tab
    F:\\new        ->  \\n 是换行
    F:\\repos      ->  \\r 是回车
    F:\\projects   ->  \\p 不是转义，Python 保留但会给 DeprecationWarning
    C:\\1test      ->  \\1 是八进制转义

判据收紧以压掉误报：**只检查看起来像 Windows 路径的字符串**——
含盘符（`X:\\`）或以 `\\\\` 开头的 UNC 路径。这样 `"\\n".join(...)` 这类正常用法
不会被报。
"""
from __future__ import annotations

import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "node_modules", ".venv", "build", "dist", "dist_new",
             ".pytest_cache", ".mypy_cache", "android", "reverse"}

# 单个反斜杠 + 一个会转义的字符
ESCAPE = re.compile(r"(?<!\\)\\([fnrtvab0-7xuUN])")
# 看起来像 Windows 路径：单个字母盘符加反斜杠（前面要有边界），或以双反斜杠开头。
# 盘符必须是**单个**字母且前面不是字母数字，否则 "running:\n" 里的 g:\ 会被误判。
WIN_PATH = re.compile(r"(?:^|[^A-Za-z0-9])[A-Za-z]:\\|^\\\\")


def walk():
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        yield path


def main() -> int:
    problems: list[tuple[str, int, str]] = []
    checked = 0
    for path in walk():
        checked += 1
        try:
            with path.open("rb") as handle:
                tokens = list(tokenize.tokenize(handle.readline))
        except (OSError, tokenize.TokenError, SyntaxError):
            continue
        for tok in tokens:
            if tok.type != tokenize.STRING:
                continue
            text = tok.string
            prefix = re.match(r"[A-Za-z]*", text).group(0).lower()
            if "r" in prefix or "b" in prefix:
                continue
            body = text[len(prefix):]
            if not WIN_PATH.search(body):
                continue          # 不像路径，放过（避免 \\n\\t 的正常用法误报）
            for match in ESCAPE.finditer(body):
                snippet = body[max(0, match.start() - 10):match.end() + 10]
                problems.append((str(path.relative_to(ROOT)), tok.start[0],
                                 snippet.replace("\n", "\\n")))

    print(f"检查 {checked} 个 .py 的字符串字面量（只挑像 Windows 路径的）")
    if problems:
        print(f"\n{len(problems)} 处：")
        for rel, line, snippet in problems:
            print(f"  {rel}:{line}  ...{snippet}...")
        print()
        print("改法：用正斜杠（F:/x/y），或者加 r 前缀写成 raw 字符串。")
        return 1
    print("没有「Windows 路径里含未转义反斜杠」的情况")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
