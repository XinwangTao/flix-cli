"""Tiny CLI for the IDA Pro MCP HTTP endpoint (ida-pro-mcp v1.1.3).

Usage: python mcp.py <port> <tool> '<json-args>'
       python mcp.py <port> <tool>            (args from stdin)
"""
import json
import sys
import urllib.request


def call(port: int, name: str, args: dict) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        body = r.read().decode("utf-8", "replace")
    outer = json.loads(body)
    result = outer.get("result", {})
    if result.get("isError"):
        return {"error": result.get("content", [{}])[0].get("text", "")}
    text = result["content"][0]["text"]
    try:
        return json.loads(text)
    except Exception:
        return {"text": text}


if __name__ == "__main__":
    port = int(sys.argv[1])
    name = sys.argv[2]
    args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else json.loads(sys.stdin.read())
    out = call(port, name, args)
    print(json.dumps(out, ensure_ascii=False, indent=1))
