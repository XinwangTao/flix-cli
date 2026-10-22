"""Delta-match Dart pool entries against .text pool-load disps (fixed).

Relation: disp = slot_addr - pool_base, so for two pool slots:
    disp_a - disp_b == slot_a - slot_b.
For an anchor string A and any other string B referenced by the same pool
(= same function family), the .text pool loads include a pair whose disp
difference equals the slot-address difference of A's and B's entries.

Method: build all within-window load pairs (12.5M), sort their disp deltas,
then for each needed delta (anchor slot x other slot) binary-search matches.

Outputs JSON: {anchor: {other_string: [[addr_load_a, addr_load_b], ...]}}
"""
import ctypes
import json
import sys

import numpy as np

sys.path.insert(0, r"D:\flix-cli\reverse")
from mempool import open_proc, regions, read  # noqa: E402

WINDOW = 6144
MAX_OCC = 16
MAX_MATCHES_PER_DELTA = 8


def resolve_all_occurrences(blobs, probes):
    objs = {}
    for base, blob in blobs:
        for s in probes:
            nb = s.encode()
            i = blob.find(nb)
            cnt = 0
            while i != -1 and cnt < MAX_OCC:
                if i >= 16:
                    ln = int.from_bytes(blob[i - 8:i - 4], "little")
                    if ln == len(s) * 2:
                        objs.setdefault(s, []).append(base + i - 16)
                        cnt += 1
                i = blob.find(nb, i + 1)
    return objs


def main():
    pid = int(sys.argv[1])
    probes_file, out_path, anchors_file = sys.argv[2], sys.argv[3], sys.argv[4]
    probes = [s.strip() for s in open(probes_file, encoding="utf-8").read().splitlines() if s.strip()]
    anchors = [s.strip() for s in open(anchors_file, encoding="utf-8").read().splitlines() if s.strip()]

    loads = json.load(open(r"D:\flix-cli\reverse\code_loads.json"))
    la = np.array(sorted(int(k, 16) for k in loads), dtype=np.int64)
    ld = np.array([loads[hex(a)] for a in la], dtype=np.int64)
    print(f"loads: {len(la)}", file=sys.stderr)

    h = open_proc(pid)
    blobs = [(b, read(h, b, s)) for b, s in regions(h) if s <= 512 * 1024 * 1024]
    blobs = [(b, bl) for b, bl in blobs if bl]
    objs = resolve_all_occurrences(blobs, probes)
    n_obj = sum(len(v) for v in objs.values())
    print(f"objects resolved: {n_obj} for {len(objs)} strings", file=sys.stderr)

    # slots: heap addresses of tagged references to probe objects
    tagged = {a + 1: s for s, v in objs.items() for a in v}
    tkeys = np.array(sorted(tagged.keys()), dtype=np.uint64)
    slots = {}   # slot_addr -> string
    for base, blob in blobs:
        n = len(blob) // 8 * 8
        arr = np.frombuffer(blob[:n], dtype=np.uint64)
        hit = np.isin(arr, tkeys)
        for i in np.nonzero(hit)[0]:
            slots[base + i * 8] = tagged[int(arr[i])]
    print(f"slots: {len(slots)}", file=sys.stderr)

    # within-window load pairs
    pai, pbi = [], []
    for w in range(1, 600):
        idx = np.nonzero((la[w:] - la[:-w]) <= WINDOW)[0]
        pai.append(idx)
        pbi.append(idx + w)
    pai = np.concatenate(pai)
    pbi = np.concatenate(pbi)
    pdelta = ld[pbi] - ld[pai]
    order = np.argsort(pdelta, kind="stable")
    pdelta_sorted = pdelta[order]
    print(f"window pairs: {len(pai)}", file=sys.stderr)

    # slots by string
    slots_by_str = {}
    for sa, s in slots.items():
        slots_by_str.setdefault(s, []).append(sa)

    result = {}
    other_slots = sorted(slots.keys())
    other_arr = np.array(other_slots, dtype=np.int64)
    other_str = np.array([slots[a] for a in other_slots])

    for anchor in anchors:
        occ = set(objs.get(anchor, []))
        a_slots = [sa for sa in other_slots if (sa in slots) and slots[sa] == anchor]
        # note: slots[sa] is the string; anchor slots are those storing anchor
        per_other = {}
        for sa in a_slots:
            deltas = sa - other_arr
            keep = other_str != anchor
            for d, ostr in zip(deltas[keep].tolist(), other_str[keep].tolist()):
                lo = np.searchsorted(pdelta_sorted, d, side="left")
                hi = np.searchsorted(pdelta_sorted, d, side="right")
                if hi <= lo:
                    continue
                for k in order[lo:min(hi, lo + MAX_MATCHES_PER_DELTA)].tolist():
                    per_other.setdefault(ostr, []).append([int(la[pai[k]]), int(la[pbi[k]])])
        result[anchor] = per_other
        print(f"anchor {anchor!r}: {len(per_other)} co-strings", file=sys.stderr)

    json.dump(result, open(out_path, "w"), indent=1)
    print("done", file=sys.stderr)


if __name__ == "__main__":
    main()
