"""Generic Dart AOT function string extractor.

For each code window (grouped pool loads):
1. Vote a pool base B via probe-slot deltas: window pair (d1,d2) matches
   slot pair (s1,s2) iff d1-d2 == s1-s2; B = s1 - d1.
2. Verify B: for the window's disps, heap qword at B+disp must be a tagged
   pointer to a readable ASCII string object for >=70% of disps.
3. Walk the pool from B+7, resolve strings, map window loads -> strings.

Usage: python func_strings.py <pid> <out.json> [window=2048]
"""
import ctypes
import json
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, r"D:\flix-cli\reverse")
from mempool import open_proc, regions, read  # noqa: E402


def resolve_probe_slots(blobs, probes):
    objs = {}
    for base, blob in blobs:
        for s in probes:
            nb = s.encode()
            i = blob.find(nb)
            cnt = 0
            while i != -1 and cnt < 12:
                if i >= 16 and int.from_bytes(blob[i - 8:i - 4], "little") == len(s) * 2:
                    objs.setdefault(s, []).append(base + i - 16)
                    cnt += 1
                i = blob.find(nb, i + 1)
    tagged = {a + 1: s for s, v in objs.items() for a in v}
    tkeys = np.array(sorted(tagged.keys()), dtype=np.uint64)
    slots = {}
    for base, blob in blobs:
        n = len(blob) // 8 * 8
        arr = np.frombuffer(blob[:n], dtype=np.uint64)
        hit = np.isin(arr, tkeys)
        for i in np.nonzero(hit)[0]:
            slots[base + i * 8] = tagged[int(arr[i])]
    return slots


def main():
    pid = int(sys.argv[1])
    out_path = sys.argv[2]
    window = int(sys.argv[3]) if len(sys.argv) > 3 else 2048
    probes = [s.strip() for s in open(r"D:\flix-cli\reverse\probes.txt", encoding="utf-8").read().splitlines() if s.strip()]

    h = open_proc(pid)
    blobs = [(b, read(h, b, s)) for b, s in regions(h) if s <= 512 * 1024 * 1024]
    blobs = [(b, bl) for b, bl in blobs if bl]
    heap = dict(blobs)

    def qword(addr):
        for base, blob in ((heap.get(0), None),):
            pass
        # fast path: find containing blob via bisect
        import bisect
        i = bisect.bisect_right(bases_list, addr) - 1
        if i >= 0:
            base, blob = blobs[i]
            off = addr - base
            if 0 <= off <= len(blob) - 8:
                return int.from_bytes(blob[off:off + 8], "little")
        return None

    bases_list = sorted(b for b, _ in blobs)

    def read_string_at(obj_addr):
        head = qword(obj_addr)  # first 8 bytes: tags+hash
        if head is None:
            return None
        v = qword(obj_addr + 8)
        if v is None:
            return None
        ln = v >> 32 if False else (v & 0xFFFFFFFF)
        # length is Smi u32 at obj+8 low half (we saw [tags][hash][len<<1][pad])
        # obj+8 qword = [len<<1][pad]; take low 32 bits
        ln = v & 0xFFFFFFFF
        if ln == 0 or ln % 2 or ln > 8192:
            return None
        n = ln // 2
        import bisect
        i = bisect.bisect_right(bases_list, obj_addr + 16) - 1
        if i < 0:
            return None
        base, blob = blobs[i]
        off = obj_addr + 16 - base
        if off < 0 or off + n > len(blob):
            return None
        data = blob[off:off + n]
        try:
            return data.decode("ascii")
        except UnicodeDecodeError:
            return None

    slots = resolve_probe_slots(blobs, probes)
    print(f"probe slots: {len(slots)}", file=sys.stderr)

    # delta -> [(s1, s2)] from probe slots
    sarr = sorted(slots.keys())
    delta_map = defaultdict(list)
    for i in range(len(sarr)):
        for j in range(i + 1, len(sarr)):
            delta_map[sarr[j] - sarr[i]].append((sarr[i], sarr[j]))
    print(f"probe deltas: {len(delta_map)}", file=sys.stderr)

    loads = json.load(open(r"D:\flix-cli\reverse\code_loads.json"))
    wins = defaultdict(list)
    for a, d in loads.items():
        wins[int(a, 16) // window].append((int(a, 16), d))

    results = {}
    for wid, items in sorted(wins.items()):
        if len(items) < 4:
            continue
        dispset = {d for _, d in items}
        # candidate bases via delta matches between window disp pairs
        votes = defaultdict(int)
        ds = sorted(dispset)
        for i in range(len(ds)):
            for j in range(i + 1, len(ds)):
                for s1, s2 in delta_map.get(ds[j] - ds[i], ()):
                    votes[s1 - ds[i]] += 1
        best = None
        for B, v in sorted(votes.items(), key=lambda kv: -kv[1])[:6]:
            ok = 0
            for d in dispset:
                val = qword(B + d)
                if val is not None and (val & 1) and read_string_at(val - 1):
                    ok += 1
            frac = ok / len(dispset)
            if best is None or frac > best[1]:
                best = (B, frac)
        if not best or best[1] < 0.7:
            continue
        B, frac = best
        maxd = max(dispset)
        n = (maxd - 7) // 8 + 2
        start = B + 7
        strings = {}
        for i in range(n):
            val = qword(start + i * 8)
            if val is None:
                break
            if (val & 1) and val > 0x10000:
                s = read_string_at(val - 1)
                if s:
                    strings[7 + i * 8] = s
        mapped = {}
        for a, d in items:
            s = strings.get(d)
            if s:
                mapped[hex(a)] = s
        results[hex(wid * window)] = {"pool": hex(B), "cover": round(frac, 2),
                                      "strings": mapped}
    json.dump(results, open(out_path, "w"), indent=1)
    print(f"windows mapped: {len(results)}/{len(wins)}", file=sys.stderr)
    print("done", file=sys.stderr)


if __name__ == "__main__":
    main()
