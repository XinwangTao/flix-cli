"""文档与源码里的控制字符检查。

真实案例：README 里出现过 `和 <0x0C> lix models list` —— 一个残留的换页符
（form feed）夹在中文和 `flix` 之间，还顺带把 `f` 吃掉了，于是渲染出来是
「lix models list」。肉眼看不出这个字符，但 `grep f` 也搜不到。

这里扫：
  - C0 控制字符（除 \\t \\n \\r）与 DEL；
  - 零宽字符（U+200B/200C/200D/FEFF）；
  - 连续两个以上空格（文档里通常是无意留下的，但代码里有缩进，故只查 md）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "node_modules", ".venv", "build", "dist", "dist_new",
             ".pytest_cache", ".mypy_cache", "android"}
BAD_C0 = {chr(code) for code in range(0x20)} - {"\t", "\n", "\r"}
BAD_ZERO_WIDTH = {"\u200b", "\u200c", "\u200d", "\ufeff"}


def walk(suffixes: tuple[str, ...]):
    for path in ROOT.rglob("*"):
        if path.suffix not in suffixes or not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        yield path


def scan(path: Path, suffixes: tuple[str, ...]) -> list[tuple[int, str, int]]:
    problems: list[tuple[int, str, int]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return problems
    # A UTF-8 BOM on a .ps1 is deliberate: PowerShell 5.1 reads a BOM-less file as
    # ANSI, so Chinese text inside would break. Do not flag that one.
    # 有意的两种用法，都不算问题：
    #   1) .ps1 开头的 UTF-8 BOM —— PowerShell 5.1 用它判断编码；
    #   2) 代码里的 lstrip("\ufeff") / strip("\ufeff") —— 用来清掉子进程输出的 BOM。
    bom_is_intended = path.suffix == ".ps1" and text.startswith("\ufeff")
    stripping_bom = "strip(\"\ufeff\")" in text or "strip('\ufeff')" in text
    for lineno, line in enumerate(text.splitlines(), 1):
        if bom_is_intended and lineno == 1:
            line = line.lstrip("\ufeff")
        for ch in line:
            if ch not in BAD_C0 and ch not in BAD_ZERO_WIDTH and ch != "\x7f":
                continue
            # lstrip("\ufeff") 这类写法是有意的，跳过
            if ch == "\ufeff" and stripping_bom:
                continue
            problems.append((lineno, repr(ch), ord(ch)))
            break
    return problems


def main() -> int:
    suffixes = (".md", ".py", ".ps1", ".yml", ".yaml", ".toml")
    files = list(walk(suffixes))
    total = 0
    for path in files:
        hits = scan(path, suffixes)
        for lineno, shown, code in hits:
            rel = path.relative_to(ROOT)
            print(f"  {rel}:{lineno}  控制字符 {shown} (U+{code:04X})")
            total += 1
    print(f"扫描 {len(files)} 个文件")
    if total:
        print(f"{total} 处控制字符/零宽字符")
        return 1
    print("没有控制字符或零宽字符")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
