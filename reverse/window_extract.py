"""Window-based pool voting + string extraction (func_extract with manual bounds).
Usage: python window_extract.py <pid> <lo_hex> <hi_hex> [window_half]
"""
import bisect, ctypes, json, sys
from collections import defaultdict
sys.path.insert(0, r"D:\flix-cli\reverse")
from mempool import open_proc, regions, read
from func_strings import resolve_probe_slots

pid = int(sys.argv[1])
lo0, hi0 = int(sys.argv[2], 16), int(sys.argv[3], 16)
half = int(sys.argv[4], 16) if len(sys.argv) > 4 else 0x300
lo, hi = lo0 - half, hi0 + half

loads = json.load(open(r"D:\flix-cli\reverse\code_loads.json"))
h = open_proc(pid)
blobs = [(b, read(h, b, s)) for b, s in regions(h) if s <= 512*1024*1024 and s]
bbases = [b for b, _ in blobs]
probes = [s.strip() for s in open(r"D:\flix-cli\reverse\probes.txt", encoding="utf-8").read().splitlines() if s.strip()]
slots = resolve_probe_slots(blobs, probes)
sarr = sorted(slots.keys())
delta_map = defaultdict(list)
for i in range(len(sarr)):
    for j in range(i+1, len(sarr)):
        delta_map[sarr[j]-sarr[i]].append((sarr[i], sarr[j]))

def qword(addr):
    i = bisect.bisect_right(bbases, addr) - 1
    if i >= 0:
        base, blob = blobs[i]
        off = addr - base
        if 0 <= off <= len(blob) - 8:
            return int.from_bytes(blob[off:off+8], "little")
    return None

def read_str(obj):
    v = qword(obj + 8)
    if v is None: return None
    ln = v & 0xFFFFFFFF
    if not ln or ln % 2 or ln > 65536: return None
    n = ln // 2
    i = bisect.bisect_right(bbases, obj + 16) - 1
    if i < 0: return None
    base, blob = blobs[i]
    off = obj + 16 - base
    if off < 0 or off + n > len(blob): return None
    try: return blob[off:off+n].decode("ascii")
    except UnicodeDecodeError: return None

items = sorted((int(a,16), d) for a, d in loads.items() if lo <= int(a,16) < hi)
disps = sorted({d for _, d in items})
print(f"window {lo:#x}-{hi:#x}: {len(items)} loads, {len(disps)} disps", file=sys.stderr)
votes = defaultdict(int)
for x in range(len(disps)):
    for y in range(x+1, len(disps)):
        for s1, s2 in delta_map.get(disps[y]-disps[x], ()):
            votes[s1-disps[x]] += 1
best = None
for B, v in sorted(votes.items(), key=lambda kv: -kv[1])[:8]:
    ok = sum(1 for d in disps if (lambda val: val is not None and (val & 1) and read_str(val-1))(qword(B+d)))
    frac = ok / len(disps)
    if best is None or frac > best[1]: best = (B, frac)
if not best or (best[1] < 0.6 and "--force" not in sys.argv):
    print(f"no pool bound (best {best})", file=sys.stderr) if best else print("no votes", file=sys.stderr)
    sys.exit(2)
B, frac = best
maxd = max(disps)
strings = {}
start = B + 7
for k in range((maxd-7)//8 + 2):
    val = qword(start + k*8)
    if val is None: break
    if (val & 1) and val > 0x10000:
        s = read_str(val - 1)
        if s: strings[7+k*8] = s
seq = []
for x, d in items:
    s = strings.get(d)
    if s and (not seq or seq[-1][1] != s):
        seq.append((x, s))
print(f"== window {lo:#x}-{hi:#x} pool {B:#x} cover {frac:.2f} loads {len(items)}")
for a, s in seq:
    print(hex(a), repr(s))
