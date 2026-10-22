"""Scan Dart AOT .text (via IDA MCP get_bytes chunks) for object-pool loads.

Pattern: REX(any, B=1: 41|43|45|47|49|4B|4D|4F) 8B modrm(mod=10, rm=111) disp32
         -> mov r32/r64, [r15+disp32]  (Dart compressed-pointer pool loads are movl)
Output: code_loads.json {hexcode_addr: disp}
"""
import json
import sys

sys.path.insert(0, r"D:\flix-cli\reverse")
from mcp import call  # noqa: E402

PORT = 13338
TEXT_ADDR = 0xCE4000
TEXT_SIZE = 0x9FEE80
CHUNK = 0x100000

REX_OK = {0x41, 0x43, 0x45, 0x47, 0x49, 0x4B, 0x4D, 0x4F}


def loads_in(blob: bytes, base: int):
    out = []
    n = len(blob)
    for i in range(n - 7):
        if blob[i] in REX_OK and blob[i + 1] == 0x8B:
            m = blob[i + 2]
            if (m >> 6) == 2 and (m & 7) == 7:
                disp = int.from_bytes(blob[i + 3:i + 7], "little", signed=True)
                out.append((base + i, disp, i))
    return out


def main():
    all_loads = {}
    for off in range(0, TEXT_SIZE, CHUNK):
        size = min(CHUNK, TEXT_SIZE - off)
        res = call(PORT, "get_bytes", {"regions": [{"addr": hex(TEXT_ADDR + off), "size": size}]})
        if isinstance(res, list):
            hexs = res[0]["data"]
        else:
            raise SystemExit(f"chunk @{hex(off)} failed: {str(res)[:200]}")
        blob = bytes(int(t, 16) for t in hexs.split() if t.startswith("0x"))
        for a, d, i in loads_in(blob, TEXT_ADDR + off):
            all_loads[hex(a)] = d
        print(f"{hex(TEXT_ADDR+off)}: +{len(blob)} (total {len(all_loads)})", file=sys.stderr)
    json.dump(all_loads, open(r"D:\flix-cli\reverse\code_loads.json", "w"))
    print(f"saved {len(all_loads)} pool loads", file=sys.stderr)


if __name__ == "__main__":
    main()
