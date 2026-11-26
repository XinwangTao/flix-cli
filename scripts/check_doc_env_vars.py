"""检查文档提到的环境变量，是否真的被代码读取。

写错变量名是最静默的坑：用户照着设了，程序完全不认，现象是「设了没用」。
这里把文档里的 `$env:XXX` / `RD_XXX` / `FLIX_XXX` 和源码里 `os.environ.get("XXX")`
/environ["XXX"] 的名字对一遍，列出「文档提了、代码从来不读」的。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "node_modules", ".venv", "build", "dist", "dist_new",
             ".pytest_cache", ".mypy_cache", "android"}

DOC_VAR = re.compile(r"\$env:([A-Za-z_][A-Za-z0-9_]*)|\b(RD_[A-Z0-9_]+|FLIX_[A-Z0-9_]+)\b")
# 代码里出现环境变量名的三种方式：
#   1) os.environ.get("X") / getenv("X") / environ["X"]   <- 读取
#   2) {"X": ...} 作为 env 传子进程                              <- 注入（如 FLIX_SELF_*）
#   3) 其它字符串常量里的全大写名（如日志里提到）
# 第三种一并算「代码认识这个名字」，否则像 FLIX_SELF_ALIAS 这种注入型的会被误报。
CODE_VAR = re.compile(r"""["']([A-Z][A-Z0-9_]{3,})["']""")


def walk(suffixes: tuple[str, ...]):
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        yield path


def main() -> int:
    code: set[str] = set()
    for path in walk((".py",)):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in CODE_VAR.finditer(text):
            code.add(match.group(1))

    doc_refs: dict[str, set[str]] = {}
    for path in walk((".md",)):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in DOC_VAR.finditer(text):
            name = match.group(1) or match.group(2)
            if not name:
                continue
            # 只认真环境变量样式：全大写，或 RD_ / FLIX_ 前缀
            if not (name.isupper() or name.startswith(("RD_", "FLIX_"))):
                continue
            doc_refs.setdefault(name.upper(), set()).add(path.name)

    print(f"代码读取的环境变量 {len(code)} 个；文档提到 {len(doc_refs)} 个")

    # 排除系统变量与常见平台变量，它们不由本项目读取
    systemish = {"PYTHONPATH", "APPDATA", "PATH", "TEMP", "TMP", "HOME",
                 "USERPROFILE", "COMPUTERNAME", "HOSTNAME", "USER", "SHELL",
                 "GITHUB_TOKEN", "ANDROID_ADB", "MSYSTEM", "GITHUB_ACTIONS",
                 "RUNNER_OS", "CI", "APPDATA_BAK"}

    orphans = {name: where for name, where in doc_refs.items()
               if name not in code and name not in systemish}
    if orphans:
        print(f"\n{len(orphans)} 个环境变量文档提到了、但代码从不读取：")
        for name, where in sorted(orphans.items()):
            print(f"  {name:<24} 出现于 {', '.join(sorted(where))}")
        return 1
    print("文档提到的环境变量都能在代码里找到读取点")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
