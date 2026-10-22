"""Solve Dart AOT object-pool disp->string mapping (numpy accelerated).

1. Resolve probe-string object addresses in live flix.exe memory.
2. numpy-scan memory qwords for tagged pointers (obj+1) -> probe entries.
3. Solve pool bases pairwise: B = E1 - d1 must satisfy d1 in .text-disp set
   and (E2 - B) in the same set for a co-located entry E2.
4. Emit {pool_base: {disp: string}} and {string: [[base, disp], ...]}.

Usage: python pool_solve.py <pid> <probes-file> <out.json>
"""
import ctypes
import json
import sys

import numpy as np

sys.path.insert(0, r"D:\flix-cli\reverse")
from mempool import open_proc, regions, read  # noqa: E402

MAX_PAIRS = 40000


def load_blobs(h):
    blobs = []
    for base, size in regions(h):
        if size > 512 * 1024 * 1024:
            continue
        blob = read(h, base, size)
        if blob:
            blobs.append((base, blob))
    return blobs


def resolve_probes(blobs, probes):
    objs = {}
    for base, blob in blobs:
        for s in probes:
            if s in objs:
                continue
            nb = s.encode()
            i = blob.find(nb)
            while i != -1:
                if i >= 16:
                    ln = int.from_bytes(blob[i - 8:i - 4], "little")
                    if ln == len(s) * 2:
                        objs[s] = base + i - 16
                        break
                i = blob.find(nb, i + 1)
    return objs


def main():
    pid = int(sys.argv[1])
    probes = [s.strip() for s in open(sys.argv[2], encoding="utf-8").read().splitlines() if s.strip()]
    out_path = sys.argv[3]

    disp_arr = np.array(sorted({d for d in json.load(open(r"D:\flix-cli\reverse\code_loads.json")).values()
                                if d > 0}), dtype=np.int64)
    dispset = set(int(d) for d in disp_arr)
    print(f"probes={len(probes)} distinct disps={len(disp_arr)} max={int(disp_arr.max())}",
          file=sys.stderr)

    h = open_proc(pid)
    blobs = load_blobs(h)
    print(f"regions read: {len(blobs)} ({sum(len(b) for _, b in blobs)/1e6:.1f} MB)", file=sys.stderr)

    objs = resolve_probes(blobs, probes)
    print(f"resolved objects: {len(objs)}/{len(probes)}", file=sys.stderr)
    if not objs:
        sys.exit("no probe objects found")

    tagged = {a + 1: s for s, a in objs.items()}
    tkeys = np.array(sorted(tagged.keys()), dtype=np.uint64)
    entries = {}
    for base, blob in blobs:
        n = len(blob) // 8 * 8
        arr = np.frombuffer(blob[:n], dtype=np.uint64)
        hit = np.isin(arr, tkeys)
        for i in np.nonzero(hit)[0]:
            v = int(arr[i])
            entries[base + i * 8] = tagged[v]
    print(f"probe pool entries: {len(entries)}", file=sys.stderr)

    maxd = int(disp_arr.max())
    es = sorted(entries.keys())
    str_by_entry = entries
    base_votes = {}
    pairs = 0
    for i1, E1 in enumerate(es):
        for E2 in es[i1 + 1:]:
            delta = E2 - E1
            if delta > maxd:
                break
            pairs += 1
            if pairs > MAX_PAIRS:
                break
            ok = disp_arr[np.isin(disp_arr + delta, disp_arr)]
            for d1 in ok.tolist():
                B = E1 - int(d1)
                if B > 0:
                    base_votes.setdefault(B, set()).update((E1, E2))
    print(f"pairs considered: {pairs}; base candidates: {len(base_votes)}", file=sys.stderr)

    pools = {}
    for B, members in base_votes.items():
        mapped = {}
        for E in members:
            d = E - B
            if d in dispset:
                mapped.setdefault(d, str_by_entry[E])
        if len(mapped) >= 2:
            pools[B] = mapped
    # keep best base per group of entries (bases may nest); prefer most mapped entries
    pools = dict(sorted(pools.items(), key=lambda kv: -len(kv[1])))
    result = {}
    for B, pd in pools.items():
        for d, s in pd.items():
            result.setdefault(s, []).append([hex(B), int(d)])
    json.dump({"pools": {hex(B): {str(d): s for d, s in pd.items()} for B, pd in pools.items()},
               "by_string": result},
              open(out_path, "w"), indent=1)
    print(f"pools solved: {len(pools)}; strings mapped: {len(result)}", file=sys.stderr)


if __name__ == "__main__":
    main()
