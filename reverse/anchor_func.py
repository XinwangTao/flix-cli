"""Map the function containing an anchor's pool loads to strings.

Usage: python anchor_func.py <anchor> [pool_idx=0] [radius=1800]
"""
import bisect
import json
import sys

REVERSE = r"D:\flix-cli\reverse"
MAXD = 1 << 21


def load_pools():
    pools = {}
    for f in ("pools_full.json", "pools_full2.json", "pools2_dump.json"):
        try:
            data = json.load(open(f"{REVERSE}\\{f}"))
        except Exception:
            continue
        if f == "pools2_dump.json":
            for anchor, rec in data.items():
                for p in rec.get("pools", []):
                    pools[p["base"]] = {int(k): v for k, v in p["strings"].items()}
        else:
            for base, rec in data.items():
                pools[base] = {int(k): v for k, v in rec["strings"].items()}
    return pools


def main():
    anchor = sys.argv[1]
    pool_idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    radius = int(sys.argv[3]) if len(sys.argv) > 3 else 1800

    pools = load_pools()
    loads = json.load(open(f"{REVERSE}\\code_loads.json"))
    try:
        funcs = json.load(open(f"{REVERSE}\\ida_funcs.json"))
        fa = sorted((int(f["addr"], 16), int(f["addr"], 16) + int(f["size"], 16)) for f in funcs)
        fstarts = [a for a, _ in fa]
    except Exception:
        fa, fstarts = [], []

    # find anchor's own loads in each resolved pool
    for pname, pmap in pools.items():
        if pool_idx > 0:
            pool_idx -= 1
            continue
        disps = [d for d, s in pmap.items() if s == anchor]
        if not disps:
            continue
        code_addrs = sorted(a for d in disps for a in
                            (int(x, 16) for x, dd in loads.items() if dd == d))
        if not code_addrs:
            continue
        print(f"pool {pname}: anchor disps {[hex(d) for d in disps]} "
              f"code loads {[hex(a) for a in code_addrs[:6]]}")
        A = code_addrs[0]
        win = sorted((int(a, 16), d) for a, d in loads.items() if abs(int(a, 16) - A) <= radius)
        # best pool for this window
        best, bestn = None, 0
        for pn, pm in pools.items():
            n = sum(1 for _, d in win if d in pm)
            if n > bestn:
                best, bestn = pn, n
        pm = pools[best]
        seq = []
        for x, d in win:
            s = pm.get(d)
            if s and (not seq or seq[-1][1] != s):
                seq.append((x, s))
        print(f"  window@{A:#x} via pool {best} ({bestn}/{len(win)} loads mapped):")
        print("   " + " | ".join(s for _, s in seq[:90]))
        return
    print("anchor not found in resolved pools")


if __name__ == "__main__":
    main()
