"""核对 SECURITY.md 里「模型共享网关」那几条声明是否与实现一致。

安全文档是对外承诺，写错了比不写更糟（用户会按它判断能不能用）。
只做静态核对 + 少量真跑验证。
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "flixcli" / "model_gateway.py"
BASE = os.environ.get("RD_GATEWAY_URL", "http://127.0.0.1:18993")
TOKEN = os.environ.get("RD_GATEWAY_TOKEN", "")

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name:<34} {detail}", flush=True)


def main() -> int:
    text = SRC.read_text(encoding="utf-8")

    # 1) 默认关闭。注意不能在本进程改 APPDATA —— flixcli.config 在 import 时就把
    # PREFS_PATH 算好了，改环境变量不生效（我第一次就踩了）。用子进程问。
    import subprocess
    import tempfile
    probe = (
        "import tempfile, os, json, sys;"
        "sys.path.insert(0, r\"%s\");"
        "d=tempfile.mkdtemp();"
        "os.environ['APPDATA']=d;"
        "from flixcli.config import Store;"
        "from flixcli.model_gateway import load_config;"
        "s=Store();"
        "c=load_config(s);"
        "s.close();"
        "print(json.dumps({'enabled': bool(c['enabled']), 'port': c['port']}))"
    ) % str(ROOT / "src")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                         text=True, encoding="utf-8", errors="replace",
                         timeout=120)
    fresh = {}
    for line in (out.stdout or "").splitlines():
        if line.strip().startswith("{"):
            fresh = json.loads(line.strip())
    record("全新安装默认不开启共享", fresh.get("enabled") is False,
           f"enabled={fresh.get('enabled')}")

    # 2) 只开放三个端点：看路由注册
    routes = re.findall(r'app\.router\.add_(get|post)\("([^"]+)"', text)
    paths = {p for _, p in routes}
    expect = {"/v1/messages", "/v1/chat/completions", "/v1/models"}
    record("三个业务端点都在", expect <= paths, f"注册了 {len(paths)} 条路由")
    extra_api = {p for p in paths
                 if p.startswith("/v1/") and p not in expect}
    record("没有多余的 /v1 端点", not extra_api, f"多出 {extra_api}" if extra_api else "仅三条")

    # 3) 令牌打码（首 6 … 末 4）
    from flixcli.gui import _mask_token  # type: ignore
    masked = _mask_token("rd-abcdefghijklmnopqrstuvwxyz")
    record("令牌打码为 首6…末4", masked == "rd-abc…wxyz", masked)

    # 4) 错令牌限流：源码里要有 10 次 / 60 秒 / 30 秒锁的参数
    has_limit = bool(re.search(r"AUTH_FAIL_LIMIT\s*=\s*10", text))
    has_window = bool(re.search(r"AUTH_FAIL_WINDOW\s*=\s*60", text))
    has_lock = bool(re.search(r"AUTH_BLOCK_SECONDS\s*=\s*30", text))
    record("错令牌限流 10/60s/30s", has_limit and has_window and has_lock,
           f"limit={has_limit} window={has_window} lock={has_lock}")

    # 5) 上游令牌不下发给上游（级联时用自己的）
    record("级联改用对端令牌", 'headers["x-api-key"] = cfg["upstream_token"]' in text,
           "见 _proxy 的 headers 段")

    # 6) 真跑：无令牌 401、错令牌 401
    def call(headers: dict) -> int:
        body = json.dumps({"model": "glm-5.3-flash", "max_tokens": 4,
                           "messages": [{"role": "user", "content": "hi"}]}).encode()
        request = urllib.request.Request(
            BASE + "/v1/messages", data=body,
            headers={**headers, "content-type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                resp.read()
                return resp.status
        except urllib.error.HTTPError as exc:
            exc.read()
            return exc.code
        except Exception:  # noqa: BLE001
            return 0

    record("无令牌 -> 401", call({}) == 401)
    record("错令牌 -> 401", call({"x-api-key": "rd-wrong-token"}) == 401)

    # 7) 真跑：未知路径不被当作 API
    request = urllib.request.Request(BASE + "/v1/unknown", data=b"{}",
                                     headers={"x-api-key": TOKEN,
                                              "content-type": "application/json"})
    try:
        urllib.request.urlopen(request, timeout=30)
        code = 200
    except urllib.error.HTTPError as exc:
        code = exc.code
    except Exception:  # noqa: BLE001
        code = 0
    record("未知 /v1 路径不是 200", code != 200, f"HTTP {code}")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} 通过")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "src"))
    raise SystemExit(main())
