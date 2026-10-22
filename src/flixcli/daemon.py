"""flixd: resident daemon (discovery + device channel + desktop API)."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import DEVICE_PORT, DESKTOP_API_PORT
from .channel import PeerError, post_bubble
from .config import Store, load_or_create_identity
from .daemon_runtime import run_daemon


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="flixd",
                                 description="Flix-compatible LAN daemon")
    ap.add_argument("--device-port", type=int, default=DEVICE_PORT)
    ap.add_argument("--api-port", type=int, default=DESKTOP_API_PORT)
    ap.add_argument("--alias", default=None, help="override device alias")
    ap.add_argument("--multicast-if", default=None, metavar="IP",
                    help="interface IP for discovery multicast (default: auto)")
    ap.add_argument("--no-announce", action="store_true",
                    help="listen only; do not burst announces at startup")
    ap.add_argument("--no-clip-sync", action="store_true",
                    help="disable automatic clipboard sync to paired devices")
    ap.add_argument("--web-port", type=int, default=0, metavar="PORT",
                    help="also serve the browser send page on this port")
    args = ap.parse_args(argv)

    store = Store()
    ident = load_or_create_identity(store)
    if args.alias:
        ident = {**ident, "alias": args.alias}  # in-memory override only

    try:
        return asyncio.run(run_daemon(store, ident, args.device_port, args.api_port,
                                      announce=not args.no_announce,
                                      clip_sync=not args.no_clip_sync,
                                      web_port=args.web_port,
                                      multicast_if=args.multicast_if))
    except OSError as e:
        print(f"flixd: environment error: {e}", file=sys.stderr)
        return 5
    except KeyboardInterrupt:
        print("flixd: stopped", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
