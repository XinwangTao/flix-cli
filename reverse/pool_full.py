"""Fully walk resolved pools and map ALL .text pool loads through them.

Usage: python pool_full.py <pid> <out.json> <pool_base_hex> [...]
"""
import bisect
import ctypes
import json
import sys

import numpy as np

sys.path.insert(0, r"D:\flix-cli\reverse")
from mempool import open_proc, regions, read  # noqa: E402
from func_strings import resolve_probe_slots  # noqa: E402

MAX_DISP = 1 << 21  # 2MB pool walk bound


def main():
    pid = int(sys.argv[1])
    out_path = sys.argv[2]
    bases = [int(x, 16) for x in sys.argv[3:]]

    h = open_proc(pid)
    blobs = [(b, read(h, b, s)) for b, s in regions(h) if s <= 512 * 1024 * 1024]
    blobs = [(b, bl) for b, bl in blobs if bl]
    bbases = [b for b, _ in blobs]

    def qword(addr):
        i = bisect.bisect_right(bbases, addr) - 1
        if i >= 0:
            base, blob = blobs[i]
            off = addr - base
            if 0 <= off <= len(blob) - 8:
                return int.from_bytes(blob[off:off + 8], "little")
        return None

    def read_string_at(obj_addr):
        tags = qword(obj_addr)
        if tags is None:
            return None
        v = qword(obj_addr + 8)
        if v is None:
            return None
        ln = v & 0xFFFFFFFF
        if not ln or ln % 2 or ln > 65536:
            return None
        i = bisect.bisect_right(bbases, obj_addr + 16) - 1
        if i < 0:
            return None
        base, blob = blobs[i]
        off = obj_addr + 16 - base
        if off < 0:
            return None
        if (tags >> 12) & 0xF == 0xF:  # TwoByteString: ln bytes of UTF-16LE
            if off + ln > len(blob):
                return None
            try:
                return blob[off:off + ln].decode("utf-16-le")
            except UnicodeDecodeError:
                return None
        n = ln // 2  # OneByteString
        if off + n > len(blob):
            return None
        try:
            return blob[off:off + n].decode("ascii")
        except UnicodeDecodeError:
            return None

    loads = json.load(open(r"D:\flix-cli\reverse\code_loads.json"))
    inv = {}
    for a, d in loads.items():
        inv.setdefault(d, []).append(int(a, 16))

    out = {}
    for B in bases:
        strings = {}
        start = B + 7
        for i in range(MAX_DISP // 8):
            addr = start + i * 8
            val = qword(addr)
            if val is None:
                break
            if (val & 1) and val > 0x10000:
                s = read_string_at(val - 1)
                if s:
                    strings[7 + i * 8] = s
        # map loads: disp d -> string; verify slot B+d actually holds that string
        mapped = {}
        for d, s in strings.items():
            val = qword(B + d)
            if val is not None and (val & 1) and read_string_at(val - 1) == s:
                for a in inv.get(d, ()):
                    mapped[hex(a)] = s
        out[hex(B)] = {"n_strings": len(strings), "strings": {str(k): v for k, v in strings.items()},
                       "mapped_loads": mapped}
        print(f"pool {hex(B)}: {len(strings)} strings, {len(mapped)} loads mapped",
              file=sys.stderr)
    json.dump(out, open(out_path, "w"), indent=1)
    print("done", file=sys.stderr)


if __name__ == "__main__":
    main()
