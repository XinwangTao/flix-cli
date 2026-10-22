"""Locate Dart functions that load many candidate keys from one pool.

For each resolved pool: take its probe-key disps, scan code_loads for windows
(radius) loading >= threshold distinct key disps from that pool -> the
serializer/handler functions for that protocol domain.

Usage: python find_funcs.py [radius=1400] [threshold=4]
"""
import json
import sys
from collections import defaultdict

sys.path.insert(0, r"D:\flix-cli\reverse")

KEY_HINTS = ("data", "action", "intent", "fingerprint", "deviceModal", "alias",
             "name", "mimeType", "nameWithSuffix", "resourceId", "contentType",
             "clipboard", "body", "target", "receiver", "sender", "groupId",
             "waitingForAccept", "thumbnailBase64", "receiveBytes", "size",
             "path", "progress", "state", "content", "type", "time", "extra",
             "width", "height", "confirmReceive", "cancelReceive", "text",
             "image", "video", "file", "waitToAccepted", "inTransit", "version")


def main():
    radius = int(sys.argv[1]) if len(sys.argv) > 1 else 1400
    threshold = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    dumps = json.load(open(r"D:\flix-cli\reverse\pools_dump.json"))
    loads = json.load(open(r"D:\flix-cli\reverse\code_loads.json"))
    la = sorted((int(a, 16), d) for a, d in loads.items())

    for anchor, rec in dumps.items():
        p = rec["pools"][0]
        strings = {int(k): v for k, v in p["strings"].items()}
        key_disps = {d: s for d, s in strings.items() if s in KEY_HINTS}
        if len(key_disps) < threshold:
            continue
        # window scan over loads
        hits = defaultdict(set)  # window_id -> set of disps
        win_addrs = defaultdict(list)
        for i, (a, d) in enumerate(la):
            if d not in key_disps:
                continue
            w = a // radius
            hits[w].add(d)
            win_addrs[w].append(a)
        for w, ds in sorted(hits.items()):
            if len(ds) >= threshold:
                addrs = win_addrs[w]
                names = sorted({key_disps[d] for d in ds})
                print(f"pool {p['base']} win@{hex(w*radius)}-{hex((w+1)*radius)} "
                      f"keys={len(ds)} addrs={hex(addrs[0])}..{hex(addrs[-1])}")
                print("   " + ", ".join(names))


if __name__ == "__main__":
    main()
