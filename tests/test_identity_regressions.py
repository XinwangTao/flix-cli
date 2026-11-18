"""Identity regressions: build marker is a value, addresses map to devices."""
import tempfile
import unittest
from pathlib import Path

from flixcli.config import Store
from flixcli.routing import resolve_target

try:  # the unit-test job does not install the GUI extra
    from flixcli import gui
except Exception:  # noqa: BLE001
    gui = None


class BuildIdentityTests(unittest.TestCase):
    def test_app_build_is_a_value(self):
        if gui is None:
            self.skipTest("PySide6 not installed")
        self.assertIsInstance(gui.APP_BUILD, str)
        self.assertNotEqual(gui.APP_BUILD, "")


class TargetResolutionTests(unittest.TestCase):
    def test_address_of_a_known_device_maps_to_its_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db.sqlite")
            try:
                store.upsert_device("fp-phone-1", alias="PKJ110", port=8891,
                                    ip="10.0.0.9")
                self.assertEqual(resolve_target(store, "10.0.0.9:8891"),
                                 ("10.0.0.9", 8891, "fp-phone-1"))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
