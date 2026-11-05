"""Run the release updater from a bare checkout (no `pip install -e .` needed).

The desktop app does this in-process on launch; this entry point exists for
source checkouts and headless debugging, where there is no `flix` on PATH:

    python scripts\\autoupdate.py --check     # is there a newer release?
    python scripts\\autoupdate.py            # update now (Windows)
    python scripts\\autoupdate.py --dry-run   # download + verify, no swap

Prefer `RelayDesk.exe update` when a packaged build exists: it needs no Python.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from flixcli.cli import main  # noqa: E402  (path setup must run first)

if __name__ == "__main__":
    sys.exit(main(["update", *sys.argv[1:]]))
