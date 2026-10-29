"""File bubbles must carry the landed path, otherwise the UI cannot show images."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flixcli.config import Store


class SentFileRecordTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "relaydesk.db")

    def tearDown(self):
        self.store.con.close()
        self._tmp.cleanup()

    def test_sent_file_shows_up_with_its_path(self):
        from flixcli.desktop_api import _record_sent_file
        src = Path(self._tmp.name) / "photo.png"
        src.write_bytes(b"x")
        _record_sent_file(self.store, {"fingerprint": "fp-me"},
                          {"id": "bid-1", "name": "photo.png", "size": 1,
                           "to": "fp-peer"}, str(src))
        bubble = {b["id"]: b for b in self.store.bubbles(10)}["bid-1"]
        self.assertEqual(bubble["from"], "fp-me")
        self.assertEqual(bubble["to"], "fp-peer")
        self.assertEqual(bubble["content"]["path"], str(src))


class BubblePathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "relaydesk.db")

    def tearDown(self):
        self.store.con.close()
        self._tmp.cleanup()

    def _add(self, bid: str, name: str, path: str | None, state: int = 2):
        self.store.con.execute(
            "INSERT INTO bubble_entities (id, from_device, to_device, type, time)"
            " VALUES (?,?,?,?,?)", (bid, "fp-a", "fp-me", 3, 1))
        self.store.con.execute(
            "INSERT INTO file_contents (id, name, mime_type, name_with_suffix, size,"
            " state, progress, path, width, height) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (bid, name, "image/png", name, 100, state, 1.0, path, 0, 0))
        self.store.con.commit()

    def test_landed_file_exposes_path(self):
        self._add("a", "shot.png", "C:/x/shot.png")
        content = {b["id"]: b for b in self.store.bubbles(10)}["a"]["content"]
        self.assertEqual(content["path"], "C:/x/shot.png")
        self.assertEqual(content["name"], "shot.png")
        self.assertEqual(content["state"], 2)

    def test_unlanded_file_has_no_path(self):
        self._add("b", "big.zip", None, state=1)
        content = {b["id"]: b for b in self.store.bubbles(10)}["b"]["content"]
        self.assertNotIn("path", content)


if __name__ == "__main__":
    unittest.main()
