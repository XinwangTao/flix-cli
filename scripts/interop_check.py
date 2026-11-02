"""Interop check against a NATIVE Flix client (official app).

Sends the exact wire shapes captured from the official app (reverse/protocol.md
§3-§4, verbatim) to a running official client and, optionally, verifies our
receiving side by posing AS the official sender.

Usage:
  python scripts/interop_check.py --host 127.0.0.1:8891            # 我们 -> 官方
  python scripts/interop_check.py --host 127.0.0.1:8891 --self-test  # 官方格式 -> 我们

Exit code 0 = every checked item passed.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
import uuid

CONTENT_TYPE = "application/json"


def post(base: str, path: str, payload, raw: bytes | None = None):
    data = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": CONTENT_TYPE})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.read().decode("utf-8", "replace")


def check_we_to_official(base: str, my_fp: str, official_fp: str) -> bool:
    ok = True
    # 文本：官方收到应答 200 "received bubble"
    st, body = post(base, "/bubble",
                    {"id": str(uuid.uuid4()), "from": my_fp, "to": official_fp,
                     "type": 0, "content": f"interop-{int(time.time())}",
                     "time": int(time.time() * 1000), "groupId": ""})
    print(f"[we->official] text    : {st} {body!r}")
    ok &= st == 200 and "received bubble" in body

    # 剪贴板：TransIntent verbatim（extra.text 二次 JSON 编码）
    intent = {"deviceId": my_fp, "bubbleId": "", "action": "clipboard",
              "extra": {"text": json.dumps(
                  {"text": f"interop-clip-{int(time.time())}",
                   "time": int(time.time() * 1000)})}}
    st, body = post(base, "/intent", intent)
    print(f"[we->official] clip    : {st} {body!r}")
    ok &= st == 200 and "{0: success}" in body
    return ok


def check_official_to_us(base: str, my_fp: str, official_fp: str,
                         save_dir: str | None) -> bool:
    ok = True
    bid = str(uuid.uuid4())
    text = f"official-probe-{int(time.time())}"
    st, body = post(base, "/bubble",
                    {"id": bid, "from": official_fp, "to": my_fp, "type": 0,
                     "content": text, "time": int(time.time() * 1000),
                     "groupId": ""})
    print(f"[official->we] text    : {st} {body!r}")
    ok &= st == 200 and "received bubble" in body

    clip = f"official-clip-{int(time.time())}"
    st, body = post(base, "/intent",
                    {"deviceId": official_fp, "bubbleId": "", "action": "clipboard",
                     "extra": {"text": json.dumps(
                         {"text": clip, "time": int(time.time() * 1000)})}})
    print(f"[official->we] clip    : {st} {body!r}")
    ok &= st == 200 and "{0: success}" in body

    rid = str(uuid.uuid4())
    payload = b"official-to-flixcli-file"
    st, body = post(base, "/bubble",
                    {"id": rid, "from": official_fp, "to": my_fp, "type": 3,
                     "content": {"state": 2, "progress": 0.0, "speed": 0,
                                 "receiveBytes": 0,
                                 "meta": {"name": "interop_probe.txt",
                                          "mimeType": "text/plain",
                                          "nameWithSuffix": "interop_probe.txt",
                                          "androidContentUri": "",
                                          "size": len(payload), "width": 0,
                                          "height": 0, "resourceId": rid},
                                 "waitingForAccept": True},
                     "time": int(time.time() * 1000), "groupId": ""})
    print(f"[official->we] bubble3 : {st} {body!r}")
    ok &= st == 200 and "received bubble" in body

    req = urllib.request.Request(
        base + f"/file?share_id={rid}&file_name=interop_probe.txt"
               "&mode=Instance+of+%27FileMode%27",
        data=payload, headers={"Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=10) as r:
        print(f"[official->we] file    : {r.status} "
              f"{r.read().decode('utf-8', 'replace')[:40]!r}")
        ok &= r.status == 200

    if save_dir:
        import os
        landed = os.path.join(save_dir, "interop_probe.txt")
        if os.path.isfile(landed):
            with open(landed, "rb") as fh:
                same = fh.read() == payload
            print(f"[official->we] landed  : {landed} bytes-match={same}")
            ok &= same
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1:8891",
                    help="native Flix device channel (ip[:port])")
    ap.add_argument("--my-fp", required=True, help="our fingerprint")
    ap.add_argument("--official-fp", required=True, help="the native client's fingerprint")
    ap.add_argument("--save-dir", default=None,
                    help="our save dir, to verify file landing (--self-test)")
    ap.add_argument("--self-test", action="store_true",
                    help="also pose as the official sender against OUR daemon "
                         "(pass --our-host as the target)")
    ap.add_argument("--our-host", default="127.0.0.1:18991")
    args = ap.parse_args()

    base = f"http://{args.host}"
    ok = check_we_to_official(base, args.my_fp, args.official_fp)
    if args.self_test:
        ours = f"http://{args.our_host}"
        ok &= check_official_to_us(ours, args.my_fp, args.official_fp,
                                   args.save_dir)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
