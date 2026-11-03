"""`__version__` drifted to 0.1.0 while pyproject shipped 2.2.1.

The stale value leaked into user-visible surfaces: `flix --version`, `flix self`
and the MCP `serverInfo` all reported 0.1.0, and the macOS CI smoke test only
asserted the string was printed, so nothing caught it.
"""
from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

from flixcli import __version__

ROOT = Path(__file__).resolve().parent.parent


class VersionConsistencyTests(unittest.TestCase):
    def test_matches_pyproject(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(__version__, data["project"]["version"])

    def test_not_the_stale_placeholder(self):
        self.assertNotEqual(__version__, "0.1.0")
