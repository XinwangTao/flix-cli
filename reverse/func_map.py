"""Map every pool-load in a Dart function (via IDA MCP disasm) to its string.

Requires pools_dump.json (resolved pool disp->string) — tries all pools.

Usage: python func_map.py <code_addr_hex> [radius]
"""
import json
import sys

sys.path.insert(0, r"D:\flix-cli\reverse")
from mcp import call  # noqa: E402

PORT = 13338


def main():
    addr = int(sys.argv[1], 16)
    radius = int(sys.argv[2]) if len(sys.argv) > 2 else 1600
    dumps = json.load(open(r"D:\flix-cli\reverse\pools_dump.json"))
    pools = []
    for anchor, rec in dumps.items():
        for p in rec["pools"]:
            pools.append((p, {int(k): v for k, v in p["strings"].items()}))

    r = call(PORT, "disasm", {"addr": hex(addr - radius), "max_instructions": 4000})
    lines = r["asm"]["lines"].splitlines()
    cur = []
    start = addr - radius
    for ln in lines:
        parts = ln.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            ia = int(parts[0], 16)
        except ValueError:
            continue
        text = parts[1]
        if "[r15" in text or ("[r15]" in text):
            # extract disp
            disp = None
            if "[r15]" in text:
                disp = 0
            else:
                inside = text.split("[r15+")[1].split("]")[0].rstrip("h")
                try:
                    disp = int(inside, 16)
                except ValueError:
                    pass
            s = None
            used = None
            if disp is not None:
                for p, m in pools:
                    if disp in m:
                        s = m[disp]
                        used = p["base"]
                        break
            cur.append((ia, text, disp, s, used))
        else:
            cur.append((ia, text, None, None, None))
    # print compact: only interesting lines + neighbors of pool loads
    last_str = None
    for ia, text, disp, s, used in cur:
        if disp is not None:
            mark = f"  ; {s!r} @pool {used}" if s else "  ; ?"
            print(f"{ia:x}  {text}{mark}")
        elif "call" in text or "ret" in text:
            print(f"{ia:x}  {text}")


if __name__ == "__main__":
    main()
