"""Re-locate a known pool in a NEW process via anchor strings.

Usage: python pool_rebase.py <pid> <pools3.json> <pool_hex> <anchor> [<anchor2> ...]
Finds candidate pool base B where qword(B+anchor_disp) points to the anchor
string object, verified against all anchors. Prints B.
"""
import bisect, json, sys
import numpy as np
sys.path.insert(0, r"D:\flix-cli\reverse")
from mempool import open_proc, regions, read

pid = int(sys.argv[1])
pools = json.load(open(sys.argv[2]))
pool_hex = sys.argv[3]
anchors = sys.argv[4:]
s = pools[pool_hex]['strings']
disp_of = {}
for k, v in s.items():
    if v in anchors and v not in disp_of:
        disp_of[v] = int(k)
print('anchor disps:', disp_of, file=sys.stderr)
if len(disp_of) < 2:
    sys.exit('need >=2 anchors present in pool')

h = open_proc(pid)
blobs = [(b, read(h, b, sz)) for b, sz in regions(h) if sz <= 512*1024*1024 and sz]
bbases = [b for b, _ in blobs]

def qword(addr):
    i = bisect.bisect_right(bbases, addr) - 1
    if i >= 0:
        base, blob = blobs[i]
        off = addr - base
        if 0 <= off <= len(blob) - 8:
            return int.from_bytes(blob[off:off+8], 'little')
    return None

def str_at_obj(obj):
    v = qword(obj + 8)
    if v is None: return None
    ln = v & 0xFFFFFFFF
    if not ln or ln % 2 or ln > 131072: return None
    n = ln // 2
    i = bisect.bisect_right(bbases, obj + 16) - 1
    if i < 0: return None
    base, blob = blobs[i]
    off = obj + 16 - base
    if off < 0 or off + n > len(blob): return None
    try: return blob[off:off+n].decode('utf-8')
    except UnicodeDecodeError: return None

main_anchor, main_disp = next(iter(disp_of.items()))
pat = main_anchor.encode('utf-8')
hits = []
for base, blob in blobs:
    start = 0
    while True:
        i = blob.find(pat, start)
        if i < 0: break
        obj = base + i - 16
        hits.append(obj)
        start = i + 1
print(f'{len(hits)} object hits for {main_anchor!r}', file=sys.stderr)

# for each hit, find 8-aligned slots pointing to obj+1
cands = {}
for obj in hits[:200]:
    tag = (obj + 1).to_bytes(8, 'little')
    for base, blob in blobs:
        start = 0
        while True:
            i = blob.find(tag, start)
            if i < 0: break
            if i % 8 == 0:
                slot = base + i
                cands.setdefault(slot - main_disp, 0)
                cands[slot - main_disp] += 1
            start = i + 1
print(f'{len(cands)} candidate bases', file=sys.stderr)

ok = []
for B, cnt in sorted(cands.items(), key=lambda kv: -kv[1]):
    good = 0
    for v, dp in disp_of.items():
        val = qword(B + dp)
        if val is not None and (val & 1) and str_at_obj(val - 1) == v:
            good += 1
    if good == len(disp_of):
        ok.append((B, good))
for B, good in ok:
    print(hex(B), f'{good}/{len(disp_of)}')
if not ok:
    print('NO VERIFIED BASE', file=sys.stderr)
