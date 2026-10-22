"""Resolve pool bases from delta matches, dump full pool contents, and
map every pool-load instruction around the anchor's function to strings.

Usage: python pool_resolve.py <pid> <delta_result.json> <out.json>
"""
import ctypes
import json
import sys
from collections import Counter

import numpy as np

sys.path.insert(0, r"D:\flix-cli\reverse")
from mempool import open_proc, regions, read  # noqa: E402


def read_string_at(h, obj_addr: int) -> str | None:
    head = read(h, obj_addr, 16)
    if not head or len(head) < 16:
        return None
    ln = int.from_bytes(head[8:12], "little")
    if ln == 0 or ln % 2 or ln > 2 * 4096:
        return None
    data = read(h, obj_addr + 16, ln // 2)
    if not data:
        return None
    try:
        return data.decode("ascii")
    except UnicodeDecodeError:
        return None


def main():
    pid = int(sys.argv[1])
    delta_path, out_path = sys.argv[2], sys.argv[3]
    delta = json.load(open(delta_path))
    loads = json.load(open(r"D:\flix-cli\reverse\code_loads.json"))

    h = open_proc(pid)

    # rebuild slot map (slot_addr -> string) from live memory
    probes = sorted({s for per in delta.values() for s in per} |
                    set(delta.keys()))
    blobs = [(b, read(h, b, s)) for b, s in regions(h) if s <= 512 * 1024 * 1024]
    blobs = [(b, bl) for b, bl in blobs if bl]
    objs = {}
    for base, blob in blobs:
        for s in probes:
            nb = s.encode()
            i = blob.find(nb)
            cnt = 0
            while i != -1 and cnt < 16:
                if i >= 16 and int.from_bytes(blob[i - 8:i - 4], "little") == len(s) * 2:
                    objs.setdefault(s, []).append(base + i - 16)
                    cnt += 1
                i = blob.find(nb, i + 1)
    tagged = {a + 1: s for s, v in objs.items() for a in v}
    slots = {}
    for base, blob in blobs:
        n = len(blob) // 8 * 8
        arr = np.frombuffer(blob[:n], dtype=np.uint64)
        tkeys = np.array(sorted(tagged.keys()), dtype=np.uint64)
        hit = np.isin(arr, tkeys)
        for i in np.nonzero(hit)[0]:
            slots[base + i * 8] = tagged[int(arr[i])]
    slot_of_anchor = {}  # anchor -> [slot addrs]
    for sa, s in slots.items():
        slot_of_anchor.setdefault(s, []).append(sa)
    print(f"slots: {len(slots)}", file=sys.stderr)

    out = {}
    for anchor, per_other in delta.items():
        a_slots = slot_of_anchor.get(anchor, [])
        votes = Counter()
        evidence = {}
        for other, pairs in per_other.items():
            o_slots = slot_of_anchor.get(other, [])
            for (L1, L2) in pairs:
                d1, d2 = loads.get(hex(L1)), loads.get(hex(L2))
                if d1 is None or d2 is None:
                    continue
                for sa in a_slots:
                    for sb in o_slots:
                        for da, db in ((d1, d2), (d2, d1)):
                            B1 = sa - da
                            B2 = sb - db
                            if B1 == B2 and B1 > 0:
                                votes[B1] += 1
                                evidence.setdefault(B1, []).append((L1, L2, other))
        if not votes:
            out[anchor] = {"pools": []}
            continue
        pools = []
        for B, cnt in votes.most_common(4):
            if cnt < 3:
                continue
            # B is the tagged pool pointer (r15 value); slots sit at B + disp
            # with disp ≡ 7 (mod 8); first entry slot = B + 7.
            entries = {}
            start = B + 7
            blob = None
            for base, bl in blobs:
                if base <= start < base + len(bl):
                    blob = bl
                    soff = start - base
                    break
            if blob is None:
                continue
            maxdisp = max(loads.values(), default=8192)
            n_slots = min((maxdisp - 7) // 8 + 2, 65536)
            for i in range(n_slots):
                off = soff + i * 8
                if off + 8 > len(blob):
                    break
                v = int.from_bytes(blob[off:off + 8], "little")
                s = read_string_at(h, v - 1) if (v & 1) == 1 and v > 0x10000 else None
                if s is not None:
                    entries[7 + i * 8] = s
            # anchor's own loads: disp in entries with value == anchor
            own = [hex(B + d) for d, s in entries.items() if s == anchor]
            pools.append({"base": hex(B), "votes": cnt, "entries": len(entries),
                          "own_load_addrs": own,
                          "strings": {str(d): s for d, s in sorted(entries.items())}})
        out[anchor] = {"pools": pools}
        print(f"{anchor!r}: {[ (p['base'], p['votes'], p['entries']) for p in pools ]}",
              file=sys.stderr)

    json.dump(out, open(out_path, "w"), indent=1)
    print("done", file=sys.stderr)


if __name__ == "__main__":
    main()
