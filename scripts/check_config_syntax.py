"""配置文件语法检查：这些文件只在被某个流程读到时才会暴露语法错误。

覆盖：
  - .toml  (pyproject.toml —— 版本号真源，写错了 CI 的版本断言会炸)
  - .json  (workflow 之外的各种配置)
  - .yml   (GitHub Actions —— 缩进错一个空格，推送后才发现)
  - .cmd/.bat（对端/启动用的批处理，语法错只在双击时暴露）

只做解析，不执行任何东西。缺 pyyaml 就跳过 yml 并说明。
"""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "node_modules", ".venv", "build", "dist", "dist_new",
             ".pytest_cache", ".mypy_cache", "android", "reverse"}

try:
    import yaml  # type: ignore
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


def walk(*suffixes: str):
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        yield path


def main() -> int:
    problems: list[tuple[str, str]] = []
    counts = {"toml": 0, "json": 0, "yml": 0, "cmd": 0}

    for path in walk(".toml"):
        counts["toml"] += 1
        try:
            tomllib.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            problems.append((str(path.relative_to(ROOT)), f"TOML: {exc}"))

    for path in walk(".json"):
        counts["json"] += 1
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            problems.append((str(path.relative_to(ROOT)), f"JSON: {exc}"))

    if HAVE_YAML:
        for path in walk(".yml", ".yaml"):
            counts["yml"] += 1
            try:
                yaml.safe_load(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                problems.append((str(path.relative_to(ROOT)), f"YAML: {exc}"))

    for path in walk(".cmd", ".bat"):
        counts["cmd"] += 1
        raw = path.read_bytes()
        # 批处理的两类常见事故：非 ASCII 无代码页声明、CRLF 缺失
        if any(b > 127 for b in raw) and b"chcp" not in raw:
            problems.append((str(path.relative_to(ROOT)),
                             "非 ASCII 但没有 chcp 声明代码页"))
        if b"\r\n" not in raw and b"\n" in raw:
            problems.append((str(path.relative_to(ROOT)),
                             "行尾是 LF；cmd.exe 对 LF 脚本处理不稳"))

    print(f"toml {counts['toml']} / json {counts['json']} / "
          f"yml {counts['yml']}{'' if HAVE_YAML else ' (跳过, 无 pyyaml)'} / "
          f"cmd {counts['cmd']}")
    if problems:
        print(f"\n{len(problems)} 处：")
        for rel, why in problems:
            print(f"  {rel}: {why}")
        return 1
    print("配置文件全部可解析")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
