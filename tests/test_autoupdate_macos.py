"""macOS bundle updates: payload recognition, digest, swap and rollback.

Kept separate from test_autoupdate.py, whose fixtures model the Windows
single-exe layout and skip everywhere else.
"""
from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from flixcli import autoupdate as au

MACHO = b"\xcf\xfa\xed\xfe"


def make_bundle(root: Path, marker: bytes, *, size: int = 1 << 20) -> Path:
    app = root / au.APP_NAME
    inner = app / "Contents" / "MacOS" / "RelayDesk"
    inner.parent.mkdir(parents=True, exist_ok=True)
    inner.write_bytes(MACHO + marker + b"\x00" * max(0, size - 4 - len(marker)))
    (app / "Contents" / "Info.plist").write_text("<plist/>", encoding="utf-8")
    return app


def zip_bundle(app: Path, dest: Path) -> Path:
    """Same shape as `ditto --keepParent`: RelayDesk.app/... at the root."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(app.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(app.parent))
    return dest


class HealthGateTests(unittest.TestCase):
    def test_health_is_the_binary_running_not_a_new_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / au.APP_NAME
            inner = au.bundle_executable(app)
            inner.parent.mkdir(parents=True)
            inner.write_text("#!/bin/sh\necho 'flix 9.9.9'\n", encoding="utf-8")
            inner.chmod(0o755)
            self.assertTrue(au.wait_app_healthy(app, timeout=5, expected="9.9.9"))
            # Release.version is a parsed tuple; comparing must not raise.
            self.assertTrue(au.wait_app_healthy(app, timeout=5, expected=(9, 9, 9)))
            self.assertFalse(au.wait_app_healthy(app, timeout=2, expected="1.0.0"))

    def test_health_fails_when_the_binary_cannot_execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / au.APP_NAME
            inner = au.bundle_executable(app)
            inner.parent.mkdir(parents=True)
            inner.write_text("not a program", encoding="utf-8")
            inner.chmod(0o644)
            self.assertFalse(au.wait_app_healthy(app, timeout=2))


class AssetPatternTests(unittest.TestCase):
    def test_intel_host_never_picks_the_arm64_zip(self):
        pattern = au.platform_asset_pattern("darwin", "x86_64")
        self.assertRegex("RelayDesk-v2.5.2-macos-x86_64.zip", pattern)
        self.assertNotRegex("RelayDesk-v2.5.2-macos-arm64.zip", pattern)

    def test_apple_silicon_never_picks_the_intel_zip(self):
        pattern = au.platform_asset_pattern("darwin", "arm64")
        self.assertRegex("RelayDesk-v2.5.2-macos-arm64.zip", pattern)
        self.assertNotRegex("RelayDesk-v2.5.2-macos-x86_64.zip", pattern)

    def test_windows_and_linux_keep_their_own_patterns(self):
        self.assertRegex("RelayDesk-v2.5.2-win64.zip",
                         au.platform_asset_pattern("win32", "AMD64"))
        self.assertRegex("RelayDesk-v2.5.2-linux-x86_64.zip",
                         au.platform_asset_pattern("linux", "x86_64"))


class BundlePayloadTests(unittest.TestCase):
    def test_zip_with_app_is_recognised_as_a_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = make_bundle(root / "src", b"v1")
            archive = zip_bundle(app, root / "release.zip")
            payload = au.extract_payload(archive, root / "staging")
            self.assertEqual(payload.kind, "app")
            self.assertTrue(au.bundle_executable(payload.path).is_file())
            au.validate_payload(payload)

    def test_zip_without_the_macho_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "broken.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("RelayDesk.app/Contents/Info.plist", "<plist/>")
            with self.assertRaises(au.UpdateError):
                au.extract_payload(archive, root / "staging")

    def test_extracted_bundle_keeps_the_executable_bit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = make_bundle(root / "src", b"v1")
            au.bundle_executable(app).chmod(0o644)  # what zipfile would give us
            archive = zip_bundle(app, root / "release.zip")
            payload = au.extract_payload(archive, root / "staging")
            inner = au.bundle_executable(payload.path)
            self.assertTrue(inner.stat().st_mode & 0o111,
                            "解包后丢了可执行位，装完的应用无法启动")
            au.validate_payload(payload)

    def test_zip_symlinks_are_restored_as_links(self):
        import stat as stat_mod
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = make_bundle(root / "src", b"v1")
            frameworks = app / "Contents" / "Frameworks"
            frameworks.mkdir(parents=True)
            (frameworks / "Python").write_bytes(b"placeholder")
            archive = root / "release.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
                for path in sorted(app.rglob("*")):
                    if path.is_file():
                        zf.write(path, path.relative_to(app.parent))
                link = zipfile.ZipInfo("RelayDesk.app/Contents/Frameworks/Link")
                link.external_attr = (stat_mod.S_IFLNK | 0o777) << 16
                zf.writestr(link, "../MacOS/RelayDesk")
            payload = au.extract_payload(archive, root / "staging")
            restored = payload.path / "Contents" / "Frameworks" / "Link"
            self.assertTrue(restored.is_symlink(), "符号链接被解成了普通文件")
            self.assertEqual(str(restored.readlink()), "../MacOS/RelayDesk")

    def test_non_macho_payload_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = make_bundle(root / "src", b"v1")
            inner = au.bundle_executable(app)
            inner.write_bytes(b"PK\x03\x04" + b"\x00" * (1 << 20))
            archive = zip_bundle(app, root / "release.zip")
            payload = au.extract_payload(archive, root / "staging")
            with self.assertRaises(au.UpdateError):
                au.validate_payload(payload)


class BundleSwapTests(unittest.TestCase):
    def test_swap_replaces_the_bundle_and_keeps_a_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = make_bundle(root / "installed", b"old")
            new = make_bundle(root / "incoming", b"new")
            old_digest = au.target_digest(current)
            backup = au.swap_app_bundle(current, new, stamp="20260101-000000")
            self.assertTrue(backup.is_dir())
            self.assertNotEqual(au.target_digest(current), old_digest)
            self.assertEqual(au.target_digest(backup), old_digest)
            # The bundle still has its structure after the swap.
            self.assertTrue(au.bundle_executable(current).is_file())
            self.assertTrue((current / "Contents" / "Info.plist").is_file())

    def test_restore_puts_the_old_bundle_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = make_bundle(root / "installed", b"old")
            new = make_bundle(root / "incoming", b"new")
            old_digest = au.target_digest(current)
            backup = au.swap_app_bundle(current, new, stamp="20260101-000001")
            au._restore([backup], current)
            self.assertEqual(au.target_digest(current), old_digest)

    def test_detect_target_accepts_an_explicit_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = make_bundle(Path(tmp), b"v1")
            found = au.detect_target(explicit=str(app), platform="darwin")
            self.assertEqual(found, app)

    def test_detect_target_rejects_a_bundle_without_the_macho(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / au.APP_NAME
            (app / "Contents").mkdir(parents=True)
            with self.assertRaises(au.UpdateError):
                au.detect_target(explicit=str(app), platform="darwin")


if __name__ == "__main__":
    unittest.main()
