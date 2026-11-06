#!/usr/bin/env python3
"""一处改版本号：pyproject / __init__ / CHANGELOG 同步，避免三处漂移。

    python scripts/bump_version.py 2.4.0
    python scripts/bump_version.py 2.4.0 --date 2026-11-07
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
INIT = ROOT / "src" / "flixcli" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"


def bump_pyproject(version: str) -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    new, n = re.subn(r'(?m)^version = "[^"]+"', f'version = "{version}"', text, count=1)
    if n != 1:
        raise SystemExit("pyproject.toml 里没找到 version")
    PYPROJECT.write_text(new, encoding="utf-8")
    return "pyproject.toml"


def bump_init(version: str) -> str:
    text = INIT.read_text(encoding="utf-8")
    new, n = re.subn(r'__version__ = "[^"]+"', f'__version__ = "{version}"', text, count=1)
    if n != 1:
        raise SystemExit("__init__.py 里没找到 __version__")
    INIT.write_text(new, encoding="utf-8")
    return "__init__.py"


def bump_changelog(version: str, date: str) -> str:
    text = CHANGELOG.read_text(encoding="utf-8")
    if "## [未发布]" not in text:
        raise SystemExit("CHANGELOG.md 缺少 [未发布] 段")
    # 未发布 -> 具体版本，并在最前面补一个新的未发布段
    text = text.replace("## [未发布]", f"## [未发布]\n\n### 新增\n\n## [{version}] - {date}", 1)
    CHANGELOG.write_text(text, encoding="utf-8")
    return "CHANGELOG.md"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("version", help="形如 2.4.0，不带 v")
    ap.add_argument("--date", default=dt.date.today().isoformat())
    args = ap.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        raise SystemExit("版本号要 x.y.z")
    touched = [bump_pyproject(args.version), bump_init(args.version),
               bump_changelog(args.version, args.date)]
    print("bumped to", args.version, "->", ", ".join(touched))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
