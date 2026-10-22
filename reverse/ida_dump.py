"""Dump memory around an address via IDA MCP get_bytes; print strings + hexdump.

Usage: python ida_dump.py <port> <addr_hex> <size> [<back>]
"""
import json
import sys

sys.path.insert(0, r"D:\flix-cli\reverse")
from mcp import call  # noqa: E402


def printable(data: bytes, base: int, min_len: int = 4) -> list:
    out = []
    start = None
    for i, b in enumerate(data + b"\x00"):
        if 32 <= b < 127:
            if start is None:
                start = i
        else:
            if start is not None and i - start >= min_len:
                out.append((base + start, data[start:i].decode("ascii")))
            start = None
    return out


if __name__ == "__main__":
    port = int(sys.argv[1])
    addr = int(sys.argv[2], 16)
    size = int(sys.argv[3], 0)
    back = int(sys.argv[4], 0) if len(sys.argv) > 4 else 0
    start = addr - back
    res = call(port, "get_bytes", {"regions": [{"addr": hex(start), "size": size}]})
    if "error" in res:
        print("ERROR:", res["error"])
        sys.exit(1)
    content = res if isinstance(res, list) else res.get("content", [{}])[0].get("text", res)
    try:
        inner = json.loads(content) if isinstance(content, str) else content
        hexs = inner[0]["data"] if isinstance(inner, list) else inner.get("data", "")
        data = bytes(int(tok, 16) for tok in hexs.split() if tok.startswith("0x"))
    except Exception:
        import re
        m = re.search(r'"data"\s*:\s*"([0-9a-fxA-F ]+)"', str(content))
        if not m:
            print(str(content)[:2000])
            sys.exit(2)
        data = bytes(int(tok, 16) for tok in m.group(1).split() if tok.startswith("0x"))
    print(f"=== strings in [{hex(start)}, {hex(start+len(data))}) ===")
    for a, s in printable(data, start):
        print(f"{a:#x}  {s}")
