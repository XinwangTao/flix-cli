"""flixd: resident daemon (discovery + device channel + desktop API)."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys

from . import DEVICE_PORT, DESKTOP_API_PORT
from .channel import PeerError, post_bubble
from .config import APP_DIR, Store, load_or_create_identity
from .daemon_runtime import run_daemon


def _acquire_singleton():
    """Hold one daemon per app dir for the process lifetime.

    Two instances share db.sqlite: both would flush the outbox (duplicate
    delivery) and overwrite each other's announced port. Same-port bind
    failures only caught the identical-port case.
    """
    APP_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = APP_DIR / "daemon.lock"
    fh = open(lock_path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Windows denies reads on a byte range locked by another process, so
        # close first and read the pid through a fresh handle.
        fh.close()
        try:
            pid = lock_path.read_text(encoding="ascii").strip()
        except OSError:
            pid = ""
        return None, pid
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()).encode("ascii"))
    fh.flush()
    return fh, str(os.getpid())


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

    lock, held_pid = _acquire_singleton()
    if lock is None:
        print(f"flixd: already running (pid {held_pid or '?'}); stop it first "
              f"(the daemon owns {APP_DIR})", file=sys.stderr)
        return 5

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
    finally:
        with contextlib.suppress(OSError):
            lock.close()


if __name__ == "__main__":
    sys.exit(main())
