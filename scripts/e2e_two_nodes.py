#!/usr/bin/env python3
"""PC ↔ 板子的双向互通实测（发现、握手、文本、文件）。

    python scripts/e2e_two_nodes.py 192.168.137.162 [--ssh quecpi-alpha]

退出码 0 = 全过；非 0 = 有失败项（打印明细）。
板子侧用 /opt/flixcli/.venv/bin/flix，PC 侧用本仓库的 venv。
"""
import argparse
import ast
import os
import subprocess
import sys
import tempfile
import time

PC_FLIX = [sys.executable, "-m", "flixcli.cli"]
BOARD_FLIX = "/opt/flixcli/.venv/bin/flix"

# Windows 控制台/管道默认用 ANSI 代码页（简体中文机器是 cp936）。脚本里的检查名和
# 剪贴板正文都带中文和 "✓"，直接 print 会在第 7 项就把自己打死：
#   UnicodeEncodeError: 'gbk' codec can't encode character '✓'
# 现场表现是跑完前 6 项后 traceback 退出，后面的剪贴板/文件项根本没执行，
# 而退出码还是 1，看不出是"没测"还是"测挂了"。两端实测脚本一律 UTF-8 输出。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # 非 TextIOWrapper（极端重定向）
        pass


def run(cmd, timeout=60):
    # 两端都是 UTF-8：板子的 Python 在 POSIX locale 下仍按 UTF-8 输出，PC 侧若按
    # 本地代码页（cp936）解码，读回中文剪贴板时会抛 UnicodeDecodeError，表现成
    # "远端什么都没输出"，把真正的编码问题藏起来。errors="replace" 保证诊断输出
    # 永远不会让脚本自己崩掉。
    p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                       encoding="utf-8", errors="replace")
    return (p.stdout or "") + (p.stderr or "")


def ssh(host, remote_cmd, timeout=90):
    return run(["ssh", "-o", "BatchMode=yes", host, remote_cmd], timeout)


def ssh_script(host, code, timeout=90):
    """Run a Python snippet on the board without fighting shell quoting."""
    import base64
    blob = base64.b64encode(code.encode("utf-8")).decode("ascii")
    return ssh(host, "echo %s | base64 -d | /opt/flixcli/.venv/bin/python -" % blob,
               timeout)


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ((" | " + detail.strip()[:160]) if detail else ""), flush=True)
    return ok



def ensure_pc_daemon():
    """板子要能主动发消息过来，PC 侧必须有 daemon 在跑；没跑就拉一个。"""
    import socket
    with socket.socket() as sk:
        sk.settimeout(1.0)
        if sk.connect_ex(("127.0.0.1", 8891)) == 0:
            return None
    proc = subprocess.Popen(PC_FLIX + ["serve", "--web-port", "8892"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        time.sleep(0.5)
        with socket.socket() as sk:
            sk.settimeout(0.5)
            if sk.connect_ex(("127.0.0.1", 8891)) == 0:
                return proc
    return proc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("board_ip")
    ap.add_argument("--ssh", default="quecpi-alpha")
    ap.add_argument("--pc-ip", default="192.168.137.1", help="板子看到的 PC 地址")
    args = ap.parse_args()
    results = []
    _pc_daemon = ensure_pc_daemon()

    out = run(PC_FLIX + ["ping", args.board_ip])
    results.append(check("PC→板 握手", "ok: True" in out, out))

    marker = "e2e-pc2board-%d" % int(time.time())
    out = run(PC_FLIX + ["send-text", args.board_ip, marker])
    results.append(check("PC→板 文本送达", "state: delivered" in out, out))

    marker2 = "e2e-board2pc-%d" % int(time.time())
    out = ssh(args.ssh, "%s send-text %s '%s'" % (BOARD_FLIX, args.pc_ip, marker2))
    results.append(check("板→PC 文本送达", "state: delivered" in out, out))

    # 按 IP 寻址的命令必须映射回已配对设备的指纹：resolve_target 曾把 IP 原样当
    # deviceId 返回，`flix shell <ip>` 明明已配对却回 not-paired，气泡的 to 字段
    # 也变成 IP（协议里应是指纹，官方客户端按它判断收件人）。
    shell_marker = "e2e-shell-%d" % int(time.time())
    out = run(PC_FLIX + ["shell", args.board_ip, "--", "echo " + shell_marker])
    results.append(check("PC→板 远程执行（按 IP 寻址，需已配对）",
                         shell_marker in out, out))

    out = run(PC_FLIX + ["discover", "--timeout", "35"])
    results.append(check("PC 发现板子广播", args.board_ip in out or "linux" in out, out))

    out = ssh(args.ssh, "%s discover --timeout 35" % BOARD_FLIX)
    results.append(check("板子发现 PC 广播", "windows" in out or args.pc_ip in out, out))

    # announce 也是心跳。对端防火墙挡住入站 8891 时，板上 /ping_v2 永远连不上，
    # 而设备仍在每 30s 广播；旧代码只在探活成功时刷新 seenAt，90s 后就把还在
    # 广播的设备画成离线。这里注入一条指向"没人监听"端口的 announce（保证探活
    # 必然失败），隔 20s（超过板上 15s 心跳节流）再发逐字节相同的一份，要求
    # 板上 seenAt 前进。
    from flixcli.discovery import build_announce
    keep_fp = "e2e-heartbeat-%d" % int(time.time())
    keep_packet = build_announce(
        {"alias": "e2e-heartbeat", "deviceModel": "Linux",
         "deviceType": "desktop", "fingerprint": keep_fp, "os": "linux"},
        port=45999)

    def put_announce() -> None:
        import socket
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sk.sendto(keep_packet, (args.board_ip, 8891))
        finally:
            sk.close()

    def board_seen_at():
        out = ssh_script(args.ssh, (
            "from flixcli.config import Store\n"
            "st = Store()\n"
            "rows = [r['seenAt'] for r in st.devices()\n"
            "        if r['fingerprint'] == %r]\n"
            "st.close()\n"
            "print(rows[0] if rows else None)\n" % keep_fp))
        last = out.strip().splitlines()[-1] if out.strip() else ""
        return int(last) if last.isdigit() else None

    put_announce()
    time.sleep(2.5)
    first_seen = board_seen_at()
    time.sleep(20)
    put_announce()
    time.sleep(2.5)
    second_seen = board_seen_at()
    results.append(check("重复 announce 续期在线状态（探活不可达仍在线）",
                         bool(first_seen) and bool(second_seen)
                         and second_seen > first_seen,
                         "first=%s second=%s" % (first_seen, second_seen)))
    ssh_script(args.ssh, (
        "from flixcli.config import Store\n"
        "st = Store()\n"
        "st.con.execute('DELETE FROM persistence_devices WHERE fingerprint=?',\n"
        "               (%r,))\n"
        "st.con.commit()\n"
        "st.close()\n"
        "print('cleaned')\n" % keep_fp))

    # 剪贴板：双向逐字符比对。中文 + 符号是重点——PC 端曾用 clip.exe 落剪贴板，
    # 控制台代码页把 UTF-16 正文变成 "????"，发送方却一直是 success。
    clip_pc2board = "e2e-剪贴板-pc2board-%d ✓" % int(time.time())
    out = run(PC_FLIX + ["clipboard", args.board_ip, clip_pc2board])
    if "sender not paired" in out:
        print("SKIP PC→板 剪贴板（PC 未在板上配对）")
    else:
        time.sleep(1.0)
        got = ssh_script(args.ssh,
                         "from pathlib import Path\n"
                         "import os\n"
                         "p = Path.home() / 'flixcli' / 'clipboard.txt'\n"
                         "print(p.read_text(encoding='utf-8') if p.exists() else '<missing>')\n")
        board_clip = got.strip().splitlines()[-1] if got.strip() else ""
        results.append(check("PC→板 剪贴板无损", board_clip == clip_pc2board,
                             "sent=%r got=%r" % (clip_pc2board, board_clip)))

    clip_board2pc = "e2e-剪贴板-board2pc-%d ✓" % int(time.time())
    out = ssh(args.ssh, "%s clipboard %s '%s'" % (BOARD_FLIX, args.pc_ip, clip_board2pc))
    if "sender not paired" in out:
        print("SKIP 板→PC 剪贴板（板子未在 PC 上配对）")
    else:
        from flixcli import clipboard as pc_clipboard
        local = None
        for _ in range(10):
            time.sleep(0.3)
            local = pc_clipboard.read_text()
            if local == clip_board2pc:
                break
        results.append(check("板→PC 剪贴板无损", local == clip_board2pc,
                             "sent=%r got=%r" % (clip_board2pc, local)))

    # 文件：需要双方已配对；未配对时算 SKIP 而不是 FAIL
    tmp = "e2e_file.txt"
    open(tmp, "w", encoding="utf-8").write("relaydesk e2e file\n")

    def board_copy_count():
        out = ssh_script(args.ssh, (
            "import os\n"
            "from flixcli.config import Store\n"
            "st = Store()\n"
            "d = st.prefs().get('saveDir') or os.path.expanduser('~/Downloads')\n"
            "print(sum(1 for n in os.listdir(d) if n.startswith('e2e_file')))\n"))
        last = out.strip().splitlines()[-1] if out.strip() else ""
        return int(last) if last.isdigit() else -1, out

    before, before_raw = board_copy_count()
    out = run(PC_FLIX + ["send", args.board_ip, tmp])
    if "sender not paired" in out:
        print("SKIP 文件传输（PC 未在板上配对）")
    else:
        results.append(check("PC→板 文件传输", '"ok": True' in out or "ok: True" in out, out))
        # 同名重发：板上已有同名文件时必须改名落盘，不能 409 拒绝
        out2 = run(PC_FLIX + ["send", args.board_ip, tmp])
        results.append(check("PC→板 同名文件重发", '"ok": True' in out2 or "ok: True" in out2, out2))
        after, after_raw = board_copy_count()
        results.append(check("板端新增两份副本", before >= 0 and after == before + 2,
                             "before=%s after=%s | %s" % (before, after, after_raw)))

        # 超长文件名：Windows 上 100 个汉字（300 字节）合法，ext4 的 NAME_MAX
        # 是 255 字节。接收端曾把 ENAMETOOLONG 吞掉、回 409 "destination
        # exists"，发送端只看到 upload-failed，文件其实一个字节都没落盘。
        # 现在接收端按本机规则收窄（保留扩展名、留出 " (n)" 位），savedAs
        # 报实际落盘名。
        long_tag = "e2e-long-%d" % int(time.time())
        long_name = long_tag + "-" + "长" * 100 + ".txt"
        long_payload = "long-name payload %d" % int(time.time())
        long_src = None
        for base in (tempfile.gettempdir(), os.path.expanduser("~")):
            try:
                candidate = os.path.join(base, long_name)
                with open(candidate, "w", encoding="utf-8") as fh:
                    fh.write(long_payload)
                long_src = candidate
                break
            except OSError as error:
                print("NOTE 本机建不出超长文件名（%s），换目录重试" % error)
        if long_src is None:
            print("SKIP PC→板 超长文件名（本机无法创建 300 字节文件名）")
        else:
            out = run(PC_FLIX + ["send", args.board_ip, long_src])
            results.append(check("PC→板 超长文件名送达",
                                 '"ok": True' in out or "ok: True" in out, out))
            got = ssh_script(args.ssh, (
                "import os\n"
                "d = os.path.expanduser('~/Downloads')\n"
                "hits = sorted(n for n in os.listdir(d) if n.startswith(%r))\n"
                "print(repr((len(hits), [len(n.encode()) for n in hits])))\n"
                "print(open(os.path.join(d, hits[-1]), encoding='utf-8').read() if hits else '')\n"
                % long_tag))
            lines = got.strip().splitlines()
            meta = lines[0] if lines else ""
            body = lines[1] if len(lines) > 1 else ""
            try:
                count, sizes = ast.literal_eval(meta) if meta else (0, [])
            except (ValueError, SyntaxError):
                count, sizes = 0, []
            ok = (count >= 1 and all(0 < s <= 255 for s in sizes)
                  and body == long_payload)
            results.append(check("板端按 NAME_MAX 收窄且内容一致", ok,
                                 "count=%s sizes=%s body=%r" % (count, sizes, body[:60])))

    # 板→PC 文件，且文件名用 Linux 合法、Windows 非法的字符（":" 与 "?"）。
    # NTFS 把 "linux:colon?.txt" 解释成文件 "linux" 的数据流：接收端曾因此把
    # O_EXCL 占位建到另一个路径上，正文全收完后再被 os.replace 以 WinError 123
    # 拒绝，发送方看到 400 upload-failed，Downloads 里多出一个空的 "linux"。
    stem = "e2e-b2p-%d" % int(time.time())
    hostile = stem + ":colon?.txt"
    payload = "board→pc file %d" % int(time.time())
    ssh_script(args.ssh,
               "from pathlib import Path\n"
               "p = Path('/tmp') / %r\n"
               "p.write_text(%r, encoding='utf-8')\n"
               "print(p.name)\n" % (hostile, payload))
    out = ssh(args.ssh, "%s send %s %r" % (BOARD_FLIX, args.pc_ip, "/tmp/" + hostile))
    if "not-paired" in out or "sender not paired" in out:
        print("SKIP 板→PC 文件传输（双方未配对）")
    else:
        results.append(check("板→PC 文件传输", '"ok": True' in out or "ok: True" in out, out))
        from flixcli.config import Store
        st = Store()
        try:
            row = st.con.execute(
                "SELECT path FROM file_contents WHERE name=? ORDER BY rowid DESC LIMIT 1",
                (hostile,)).fetchone()
        finally:
            st.close()
        saved = (row[0] if row and row[0] else "") or ""
        try:
            with open(saved, "rb") as fh:
                got = fh.read().decode("utf-8") if saved else ""
        except OSError:
            got = ""
        results.append(check("板→PC 文件落盘且内容一致", got == payload,
                             "saved=%r got=%r" % (saved, got)))
        if os.name == "nt":
            base = os.path.basename(saved)
            illegal = set('<>:"/\\|?*') & set(base)
            results.append(check("Windows 非法文件名已消毒", bool(base) and not illegal,
                                 "savedAs=%r" % base))
            stub = os.path.join(os.path.dirname(saved), stem)
            results.append(check("未残留 NTFS 数据流空壳", not os.path.exists(stub), stub))

    ok = all(results)
    print("RESULT: %d/%d 通过" % (sum(1 for r in results if r), len(results)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
