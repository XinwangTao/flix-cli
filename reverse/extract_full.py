"""Full Dart AOT pool extraction from live flix.exe.

Pass 1: identify every Dart OneByteString object in memory
        (printable run + Smi length check at data-8).
Pass 2: find every 8-byte tagged pointer to those objects (pool entries).
Pass 3: for each entry, walk the surrounding run of tagged pointers to find
        its ObjectPool object start; emit disp = entry - pool_base.

Outputs JSON: {"strings": {hexaddr: s}, "pools": {poolbase: {disp: s}}, "entry_pool": {hexentry: hexbase}}
"""
import ctypes
import json
import sys

sys.path.insert(0, r"D:\flix-cli\reverse")
from mempool import open_proc, regions, read  # noqa: E402


def is_probable_tagged_ptr(v: int) -> bool:
    return (v & 1) == 1 and 0x10000 < v < 0x7FFFFFFFFFFF


def main():
    pid = int(sys.argv[1])
    out_path = sys.argv[2]
    h = open_proc(pid)
    regs = [r for r in regions(h) if r[1] <= 512 * 1024 * 1024]
    blobs = []
    for base, size in regs:
        blob = read(h, base, size)
        if blob:
            blobs.append((base, blob))
    print(f"read {len(blobs)} regions", file=sys.stderr)

    # pass 1: strings
    strings = {}
    for base, blob in blobs:
        n = len(blob)
        i = 0
        # find printable runs quickly
        while True:
            j = i
            run_start = None
            # use bytes.translate approach per chunk for speed
            break
        # simpler: scan with translate to mark printable
        printable = bytes(1 if 32 <= b < 127 else 0 for b in range(256))
        marks = blob.translate(printable)
        pos = 0
        while True:
            k = marks.find(b"\x01", pos)
            if k == -1:
                break
            e = marks.find(b"\x00", k)
            if e == -1:
                e = n
            if e - k >= 3:
                if k >= 8:
                    ln = int.from_bytes(blob[k - 8:k - 4], "little")
                    if ln == (e - k) * 2:
                        strings[base + k - 16] = blob[k:e].decode("ascii")
            pos = e
    print(f"strings: {len(strings)}", file=sys.stderr)
    str_addrs = set(strings.keys())

    # pass 2: pool entries (tagged)
    entries = {}
    for base, blob in blobs:
        n = len(blob)
        for i in range(0, n - 8, 8):
            v = int.from_bytes(blob[i:i + 8], "little")
            a = v - 1
            if a in str_addrs:
                entries[base + i] = a
    print(f"entries: {len(entries)}", file=sys.stderr)

    # pass 3: per-entry pool base via tagged-pointer run walk
    def qword_at(b, i):
        return int.from_bytes(b[i:i + 8], "little")

    entry_pool = {}
    pools = {}
    for eaddr, saddr in entries.items():
        if eaddr in entry_pool:
            continue
        # locate containing blob
        for base, blob in blobs:
            if base <= eaddr < base + len(blob):
                i = eaddr - base
                # walk back while tagged ptrs
                s = i
                while s - 8 >= 0 and is_probable_tagged_ptr(qword_at(blob, s - 8)):
                    s -= 8
                # walk forward
                e = i + 8
                while e + 8 <= len(blob) and is_probable_tagged_ptr(qword_at(blob, e)):
                    e += 8
                run_start_vaddr = base + s
                # object pool object start: try header sizes 8 and 16
                # header check: at run_start-8 there should be a length field (Smi count) — try both
                for hdr in (8, 16, 24):
                    pbase = run_start_vaddr - hdr
                    disp = eaddr - pbase
                    pools.setdefault(pbase, {})[disp] = strings[saddr]
                    if hdr == 8:
                        entry_pool[eaddr] = pbase
                    break
                break
    print(f"pools: {len(pools)}", file=sys.stderr)
    json.dump({"strings": {hex(k): v for k, v in strings.items()},
               "entry_pool": {hex(k): hex(v) for k, v in entry_pool.items()},
               "pools": {hex(k): {str(d): s for d, s in v.items()} for k, v in pools.items()}},
              open(out_path, "w"))
    print("done", file=sys.stderr)


if __name__ == "__main__":
    main()
