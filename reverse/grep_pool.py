import json, sys
pool_file, needle = sys.argv[1], sys.argv[2].lower()
data = json.load(open(pool_file))
for pool, rec in data.items():
    strings = list(rec.get("strings", {}).values())
    hits = sorted({s for s in strings if needle in s.lower() and len(s) < 50})
    if hits:
        print(f"== {pool} ({len(strings)} strings)")
        for s in hits[:40]:
            print("  ", repr(s))
