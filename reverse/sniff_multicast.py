"""Passive UDP multicast sniffer for Flix discovery (224.0.0.168:8891).

Read-only capture: prints hexdump + best-effort JSON decode of each datagram.
"""
import socket
import struct
import sys
import json
import time

GROUP = "224.0.0.168"
PORT = 8891
DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 25.0


def hexdump(data: bytes) -> str:
    out = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{i:04x}  {hexs:<48}  {text}")
    return "\n".join(out)


def main() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("", PORT))
    except OSError as e:
        print(f"BIND FAILED: {e}")
        sys.exit(2)
    mreq = socket.inet_aton(GROUP) + socket.inet_aton("0.0.0.0")
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except OSError as e:
        print(f"JOIN FAILED: {e}")
    s.settimeout(1.0)
    print(f"listening on {GROUP}:{PORT} for {DURATION}s ...")
    deadline = time.time() + DURATION
    count = 0
    while time.time() < deadline:
        try:
            data, addr = s.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError as e:
            print(f"recv error: {e}")
            break
        count += 1
        ts = time.strftime("%H:%M:%S")
        print(f"\n=== packet #{count} from {addr[0]}:{addr[1]} len={len(data)} t={ts} ===")
        print(hexdump(data))
        try:
            obj = json.loads(data.decode("utf-8"))
            print("JSON:", json.dumps(obj, ensure_ascii=False, indent=2))
        except Exception:
            try:
                print("TEXT:", data.decode("utf-8", "replace"))
            except Exception:
                pass
    print(f"\ntotal {count} packets")


if __name__ == "__main__":
    main()
