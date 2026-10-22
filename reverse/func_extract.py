"""Per-function pool binding and string extraction (definitive).

For a function (IDA bounds), vote its object-pool base via probe-slot deltas,
verify coverage, then print every pool-load mapped to its string, in address
order. This resolves the same-disp-different-pool ambiguity.

Usage: python func_extract.py <func_addr_hex> [<func_addr_hex> ...]
"""
import bisect
import ctypes
import json
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, r"D:\flix-cli\reverse")
from mempool import open_proc, regions, read  # noqa: E402
from func_strings import resolve_probe_slots  # noqa: E402


def main():
    pid = int(sys.argv[1])
    targets = [int(a, 16) for a in sys.argv[2:]]
    loads = json.load(open(r"D:\flix-cli\reverse\code_loads.json"))
    funcs = json.load(open(r"D:\flix-cli\reverse\ida_funcs.json"))
    fa = sorted((int(f["addr"], 16), int(f["addr"], 16) + int(f["size"], 16)) for f in funcs)
    fstarts = [a for a, _ in fa]

    h = open_proc(pid)
    blobs = [(b, read(h, b, s)) for b, s in regions(h) if s <= 512 * 1024 * 1024]
    blobs = [(b, bl) for b, bl in blobs if bl]
    bbases = [b for b, _ in blobs]
    probes = [s.strip() for s in open(r"D:\flix-cli\reverse\probes.txt", encoding="utf-8").read().splitlines() if s.strip()]
    slots = resolve_probe_slots(blobs, probes)
    sarr = sorted(slots.keys())
    delta_map = defaultdict(list)
    for i in range(len(sarr)):
        for j in range(i + 1, len(sarr)):
            delta_map[sarr[j] - sarr[i]].append((sarr[i], sarr[j]))

    def qword(addr):
        i = bisect.bisect_right(bbases, addr) - 1
        if i >= 0:
            base, blob = blobs[i]
            off = addr - base
            if 0 <= off <= len(blob) - 8:
                return int.from_bytes(blob[off:off + 8], "little")
        return None

    def read_str(obj):
        v = qword(obj + 8)
        if v is None:
            return None
        ln = v & 0xFFFFFFFF
        if not ln or ln % 2 or ln > 65536:
            return None
        n = ln // 2
        i = bisect.bisect_right(bbases, obj + 16) - 1
        if i < 0:
            return None
        base, blob = blobs[i]
        off = obj + 16 - base
        if off < 0 or off + n > len(blob):
            return None
        try:
            return blob[off:off + n].decode("ascii")
        except UnicodeDecodeError:
            return None

    for fstart in targets:
        i = bisect.bisect_right(fstarts, fstart) - 1
        if i < 0 or fstart >= fa[i][1] or fa[i][0] != fstart:
            # accept nearest containing function
            if i < 0 or fstart >= fa[i][1]:
                print(f"== {fstart:#x}: not a function start", file=sys.stderr)
                continue
            fstart = fa[i][0]
        fend = fa[i][1]
        items = sorted((int(a, 16), d) for a, d in loads.items() if fstart <= int(a, 16) < fend)
        disps = sorted({d for _, d in items})
        if len(disps) < 2:
            print(f"== {fstart:#x}: {len(items)} loads (too few)")
            continue
        votes = defaultdict(int)
        for x in range(len(disps)):
            for y in range(x + 1, len(disps)):
                for s1, s2 in delta_map.get(disps[y] - disps[x], ()):
                    votes[s1 - disps[x]] += 1
        best = None
        for B, v in sorted(votes.items(), key=lambda kv: -kv[1])[:8]:
            ok = sum(1 for d in disps
                     if (lambda val: val is not None and (val & 1) and read_str(val - 1))(qword(B + d)))
            frac = ok / len(disps)
            if best is None or frac > best[1]:
                best = (B, frac)
        if not best or best[1] < 0.6:
            print(f"== {fstart:#x}-{fend:#x}: {len(items)} loads, no pool bound "
                  f"(best {best[1]:.2f})" if best else "== no votes")
            continue
        B, frac = best
        maxd = max(disps)
        strings = {}
        start = B + 7
        for k in range((maxd - 7) // 8 + 2):
            val = qword(start + k * 8)
            if val is None:
                break
            if (val & 1) and val > 0x10000:
                s = read_str(val - 1)
                if s:
                    strings[7 + k * 8] = s
        seq = []
        for x, d in items:
            s = strings.get(d)
            if s and (not seq or seq[-1][1] != s):
                seq.append((x, s))
        print(f"== {fstart:#x}-{fend:#x} pool {B:#x} cover {frac:.2f} loads {len(items)}")
        print("   " + " | ".join(s for _, s in seq[:120]))


if __name__ == "__main__":
    main()
