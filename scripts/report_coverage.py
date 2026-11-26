"""体系自评：把 verify_all 的每一项和它守的东西列出来，并指出覆盖盲区。

不是检查代码，而是检查「我们的检查够不够」。产出一张表，人读。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 每一项：脚本 -> (守什么, 靠什么手段, 已知盲区)
CATALOG = {
    "scripts/check_doc_paths.py": (
        "文档里提到的仓库内路径都存在",
        "正则抽路径 + os.path.exists",
        "外部 URL、示例写法（已豁免）",
    ),
    "scripts/check_doc_commands.py": (
        "文档里的子命令真的注册过、脚本真的存在",
        "对比 argparse choices + os.path.exists",
        "只查 models 子命令与 python 脚本，其它子命令未查",
    ),
    "scripts/check_doc_env_vars.py": (
        "文档提到的环境变量代码认识",
        "文档提取 vs 源码里的全大写字符串",
        "同名但语义不同的情况查不出",
    ),
    "scripts/check_control_chars.py": (
        "没有看不见的控制字符 / 零宽字符",
        "逐字符扫描 C0、DEL、U+200B/C/D、FEFF",
        "正常的多字节标点不查（也不需要）",
    ),
    "scripts/check_path_escapes.py": (
        "Windows 路径没写成非 raw 字符串（\\f/\\t/\\n 会被当转义）",
        "tokenize 逐字符串字面量 + 只挑像路径的",
        "只在看起来像路径时才查，故普通字符串里的误用可能漏",
    ),
    "scripts/check_missing_imports.py": (
        "用了 os./sys./json. 等却没 import",
        "ast 静态分析",
        "只查白名单里的 13 个常用模块",
    ),
    "scripts/check_ps1_syntax.ps1": (
        ".ps1 语法正确、非 ASCII 时带 BOM",
        "Parser::ParseFile 只解析不执行 + 字节检查",
        "不查 PS 运行期错误（参数错、路径不存在）",
    ),
    "scripts/check_config_syntax.py": (
        "toml/json/yml/cmd 都能被解析",
        "tomllib / json / pyyaml；cmd 查编码与行尾",
        "cmd 的语义错误查不出（只能看编码与行尾）",
    ),
    "scripts/check_checkers.py": (
        "上面这些检查器真的会失败（不是恒绿）",
        "给每个注入已知错误，确认非 0，再恢复",
        "只注入 5 类错误，不是穷举",
    ),
    "scripts/check_dist_fresh.py": (
        "exe 晚于最后一次 src 改动、版本与 pyproject 一致",
        "文件 mtime vs git 提交时间；跑 --version",
        "PyInstaller 不可复现，故不比字节",
    ),
    "scripts/check_apk_fresh.py": (
        "APK 晚于最后一次 Kotlin 改动",
        "文件 mtime vs git 提交时间",
        "没装 APK 时跳过（CI 会构建）",
    ),
    "tests/acceptance_lan_models.py": (
        "网关协议层 16 项功能（双协议/流式/工具/鉴权/并发/断开/自检页/响应头）",
        "真打运行中的网关",
        "不测 cc-switch 自己；不测手机 App 的 UI",
    ),
    "tests/check_readme_claims.py": (
        "README 里 8 条 OpenAI 兼容声明成立",
        "逐条真打网关",
        "只覆盖 README 里那一段声明",
    ),
    "tests/cdp_page_acceptance.py": (
        "手机页面 8 项（JS 执行/数据一致/四协议组合/工具调用/复制按钮）",
        "CDP 驱动真机浏览器",
        "需要手机+adb；没有就跳过",
    ),
    "tests/rd_shell_load.py": (
        "rdShell 并发（24 exec / 6 PTY）",
        "本地测试守护进程",
        "不测真实对端的极端负载",
    ),
}

BLIND = [
    "文档里的 ```bash/```powershell 代码块没有逐条执行（只查了路径与子命令存在）",
    "Windows GUI 的点击路径没有自动化（只验证过面板文字与状态逻辑）",
    "第三方手机 App（ChatBox 等）未实测，只保证了 OpenAI 兼容层的形状",
    "cc-switch 自身的行为变化（它升级后模型名/路由变了）不在覆盖内",
    "CI 只在 Linux/推送后跑；本机无法预演 GitHub Actions 的 runner 差异",
]


def main() -> int:
    src = (ROOT / "tests" / "verify_all.py").read_text(encoding="utf-8")
    steps = re.findall(r'step\("([^"]+)",\s*\["([^"]+)"', src)

    print(f"verify_all 共 {len(steps)} 项\n")
    width = max(len(name) for name, _ in steps)
    for name, script in steps:
        info = CATALOG.get(script, ("?", "?", "?"))
        print(f"  {name:<{width}}  {script}")
        print(f"  {'':<{width}}    守: {info[0]}")
        print(f"  {'':<{width}}    手段: {info[1]}")
        print(f"  {'':<{width}}    盲区: {info[2]}")

    missing = [s for _, s in steps if s not in CATALOG]
    if missing:
        print(f"\n未收录到目录里的脚本: {missing}")

    print("\n=== 体系级盲区（不是某一项的问题，是整体没覆盖的）===")
    for i, item in enumerate(BLIND, 1):
        print(f"  {i}. {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
