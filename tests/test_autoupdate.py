"""Offline tests for `flix update` (no network, no running RelayDesk).

The paths worth guarding are the ones that decide *not* to install (a wrong
"newer" call reinstalls a working build) and the ones that touch the exe on
disk (a half-applied swap leaves the user with nothing that launches).
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from flixcli import autoupdate as au

# The suite models the Windows installer: PE ("MZ") payloads and win64 asset
# names, which is what `flix update` actually replaces. On Linux/macOS CI the
# platform asset pattern deliberately picks a different zip, so these fixtures
# would be testing a build path that does not exist yet.
if sys.platform != "win32":
    raise unittest.SkipTest("auto-update swaps RelayDesk.exe; fixtures are Windows-only")

FAKE_EXE = b"MZ" + b"\x90" * (1 << 20)          # PE magic + enough bytes
FAKE_EXE_V2 = b"MZ" + b"\x91" * (1 << 20)


def make_zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, blob in entries.items():
            zf.writestr(name, blob)
    return path


def sha(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def release(tag: str, asset_name: str, digest: str, *, size: int = 4096,
            prerelease: bool = False) -> au.Release:
    return au.Release(tag=tag, name=tag, prerelease=prerelease, draft=False,
                      url=f"https://example.invalid/{tag}",
                      assets=[au.Asset(name=asset_name, size=size,
                                       url=f"https://example.invalid/{asset_name}",
                                       sha256=digest)])


class ManagedInstanceTests(unittest.TestCase):
    """Only long-lived local roles may be stopped; MCP stdio children and the
    updater's own process must survive a file swap."""

    def test_bare_launch_and_long_lived_roles_are_restartable(self):
        items = [
            {"pid": 1, "args": ""},
            {"pid": 2, "args": "serve --device-port 18991 --api-port 18998"},
            {"pid": 3, "args": "agent --reply hi"},
            {"pid": 4, "args": "gui"},
        ]
        self.assertEqual([i["pid"] for i in au.managed_instances(items, self_pid=99)],
                         [1, 2, 3, 4])

    def test_mcp_stdio_one_shot_and_self_are_left_alone(self):
        import os
        items = [
            {"pid": os.getpid(), "args": "update --quiet"},
            {"pid": 7, "args": "mcp"},
            {"pid": 8, "args": "send 10.0.0.2 file.zip"},
            {"pid": 9, "args": "devices"},
        ]
        self.assertEqual(au.managed_instances(items), [])

    def test_install_leaves_mcp_and_the_updater_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            exe = tmp_path / "RelayDesk.exe"
            exe.write_bytes(FAKE_EXE)
            downloads = tmp_path / "dl"
            downloads.mkdir()
            asset_name = "RelayDesk-v9.9.9-win64.zip"
            archive = make_zip(downloads / asset_name, {"RelayDesk.exe": FAKE_EXE_V2})
            rel = release("v9.9.9", asset_name, au.sha256_file(archive),
                          size=archive.stat().st_size)
            running = [
                {"pid": os.getpid(), "exe": str(exe), "args": "update --quiet"},
                {"pid": 777, "exe": str(exe), "args": "mcp"},
                {"pid": 888, "exe": str(exe), "args": "serve --api-port 59998"},
            ]
            with mock.patch.object(au, "fetch_releases", return_value=[rel]), \
                    mock.patch.object(au, "list_instances", return_value=running), \
                    mock.patch.object(au, "stop_instances",
                                      side_effect=lambda items: [i["pid"] for i in items]) as stop, \
                    mock.patch.object(au, "start_instance", return_value=None):
                result = au.run_update(exe=str(exe), state_path=tmp_path / "s.json",
                                       log_path=tmp_path / "u.log",
                                       downloads_dir=downloads, restart=False)
            stopped = [call.args[0] for call in stop.call_args_list]
            self.assertEqual([[i["pid"] for i in batch] for batch in stopped], [[888]])
            self.assertEqual(result["leftRunning"], [os.getpid(), 777])


class LogTests(unittest.TestCase):
    def test_broken_stdout_does_not_abort_the_update(self):
        """The GUI is stopped before the health check; its stdout pipe is gone.
        A failed echo must not skip the rollback that follows."""
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "update.log"
            with mock.patch("builtins.print", side_effect=OSError("closed pipe")):
                au.log_line("installed", path=log, echo=True)
            self.assertIn("installed", log.read_text(encoding="utf-8"))


class StartInstanceTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows command-line semantics")
    def test_quoted_agent_command_survives_a_restart(self):
        with mock.patch.object(au.subprocess, "Popen") as popen:
            popen.return_value = mock.Mock()
            au.start_instance(r"C:\Apps\RelayDesk.exe",
                              'agent --cmd "claude -p hello world"')
        self.assertEqual(popen.call_args.args[0],
                         '"C:\\Apps\\RelayDesk.exe" agent --cmd "claude -p hello world"')


class VersionTests(unittest.TestCase):
    def test_real_tag_forms(self):
        for text in ("v2.2.1", "2.2.1", "flix 2.2.1", "RelayDesk v2.2.1"):
            self.assertEqual(au.parse_version(text), (2, 2, 1), text)

    def test_build_suffix_is_not_part_of_the_version(self):
        self.assertEqual(au.parse_version("2.4.0+127"), (2, 4, 0))

    def test_missing_version_is_empty_not_zero(self):
        self.assertEqual(au.parse_version(""), ())
        self.assertEqual(au.parse_version(None), ())

    def test_compare_pads_short_versions(self):
        self.assertEqual(au.compare_versions("2.2", "2.2.0"), 0)
        self.assertEqual(au.compare_versions("v2.10.0", "v2.9.9"), 1)
        self.assertEqual(au.compare_versions("2.2.0", "2.2.1"), -1)


class ReleasePickTests(unittest.TestCase):
    def test_skips_draft_and_prerelease(self):
        draft = au.Release("v3.0.0", "draft", False, True, "")
        pre = au.Release("v2.3.0-rc1", "rc", True, False, "")
        stable = au.Release("v2.2.1", "stable", False, False, "")
        self.assertIs(au.pick_release([draft, pre, stable]), stable)
        self.assertIs(au.pick_release([pre, stable], include_prerelease=True), pre)

    def test_out_of_order_releases_pick_the_highest_version(self):
        """GitHub lists by created_at; a later backport must not hide a newer
        patch line (2.3.1) behind the first entry (a 2.3.0 rebuild)."""
        rebuild = au.Release("v2.3.0", "rebuild", False, False, "")
        newer = au.Release("v2.3.1", "newer", False, False, "")
        self.assertIs(au.pick_release([rebuild, newer]), newer)
        self.assertIs(au.pick_release([newer, rebuild]), newer)

    def test_prerelease_tie_loses_to_the_stable_tag(self):
        rc = au.Release("v2.3.0-rc1", "rc", True, False, "")
        stable = au.Release("v2.3.0", "stable", False, False, "")
        self.assertIs(au.pick_release([rc, stable], include_prerelease=True),
                      stable)

    def test_no_usable_release_raises_network_error(self):
        with self.assertRaises(au.UpdateError) as caught:
            au.pick_release([au.Release("v1", "d", False, True, "")])
        self.assertEqual(caught.exception.code, au.EXIT_NETWORK)

    def test_asset_digest_is_parsed_as_sha256(self):
        asset = au.Asset.from_api({"name": "x.zip", "size": 1,
                                   "browser_download_url": "u",
                                   "digest": "sha256:ABCDEF"})
        self.assertEqual(asset.sha256, "abcdef")

    def test_asset_without_digest_is_flagged_not_trusted(self):
        asset = au.Asset.from_api({"name": "x.zip", "size": 1,
                                   "browser_download_url": "u"})
        self.assertEqual(asset.sha256, "")

    def test_picks_platform_zip_and_ignores_other_platforms(self):
        rel = au.Release("v2.2.1", "", False, False, "", assets=[
            au.Asset("RelayDesk-v2.2.1-macos-arm64.zip", 1, "u1"),
            au.Asset("RelayDesk-v2.2.1-win64.zip", 2, "u2"),
        ])
        self.assertEqual(au.pick_asset(rel, pattern=au._PLATFORM_ASSET["win32"]).name,
                         "RelayDesk-v2.2.1-win64.zip")

    def test_explicit_pattern_wins(self):
        rel = au.Release("v9", "", False, False, "", assets=[
            au.Asset("RelayDesk-v9-win64.zip", 1, "u1"),
            au.Asset("RelayDesk-v9-linux.zip", 2, "u2")])
        self.assertEqual(au.pick_asset(rel, pattern=r"linux\.zip$").name,
                         "RelayDesk-v9-linux.zip")

    def test_no_matching_asset_names_what_was_offered(self):
        rel = au.Release("v9", "", False, False, "", assets=[au.Asset("a.tar.gz", 1, "u")])
        with self.assertRaises(au.UpdateError) as caught:
            au.pick_asset(rel, pattern=r"win64\.zip$")
        self.assertEqual(caught.exception.kind, "no-asset")
        self.assertIn("a.tar.gz", caught.exception.detail)


class DecideTests(unittest.TestCase):
    def test_recorded_digest_beats_tag_math(self):
        """A rebuilt exe under the same tag must not trigger a reinstall loop."""
        rel = release("v2.2.1", "a.zip", sha(b"archive"))
        record = {"tag": "v2.0.0", "sha256": sha(b"archive")}
        self.assertEqual(au.decide(record, "v2.0.0", rel, rel.assets[0])["status"],
                         "current")

    def test_same_tag_is_current(self):
        rel = release("v2.2.1", "a.zip", sha(b"archive"))
        self.assertEqual(au.decide({}, "v2.2.1", rel, rel.assets[0])["status"], "current")

    def test_newer_release_installs(self):
        rel = release("v2.3.0", "a.zip", sha(b"archive"))
        verdict = au.decide({}, "v2.2.1", rel, rel.assets[0])
        self.assertEqual(verdict, {"status": "update-available",
                                   "reason": "newer-release"})

    def test_unknown_local_build_installs_once(self):
        rel = release("v2.2.1", "a.zip", sha(b"archive"))
        verdict = au.decide({}, None, rel, rel.assets[0])
        self.assertEqual(verdict["reason"], "unknown-local-build")

    def test_local_newer_than_release_is_not_downgraded(self):
        rel = release("v2.2.1", "a.zip", sha(b"archive"))
        verdict = au.decide({}, "v2.4.0", rel, rel.assets[0])
        self.assertEqual(verdict["status"], "current")
        self.assertIn("local-newer", verdict["reason"])


class PayloadTests(unittest.TestCase):
    def test_single_exe_zip_is_the_shipped_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_zip(Path(tmp) / "a.zip", {"RelayDesk.exe": FAKE_EXE})
            payload = au.extract_payload(archive, Path(tmp) / "out")
            self.assertEqual(payload.kind, "exe")
            self.assertEqual(payload.path.name, "RelayDesk.exe")
            au.validate_payload(payload)

    def test_folder_zip_is_detected_as_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_zip(Path(tmp) / "a.zip", {
                "RelayDesk-v2.2.1-win64/RelayDesk.exe": FAKE_EXE,
                "RelayDesk-v2.2.1-win64/readme.txt": b"hi"})
            payload = au.extract_payload(archive, Path(tmp) / "out")
            self.assertEqual(payload.kind, "dir")
            self.assertEqual(payload.path.name, "RelayDesk-v2.2.1-win64")

    def test_zip_without_the_exe_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_zip(Path(tmp) / "a.zip", {"setup.msi": b"MZ"})
            with self.assertRaises(au.UpdateError) as caught:
                au.extract_payload(archive, Path(tmp) / "out")
            self.assertEqual(caught.exception.kind, "no-payload")

    def test_truncated_exe_is_rejected_before_it_reaches_disk_target(self):
        """An HTML error page saved as .exe must not replace a working build."""
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_zip(Path(tmp) / "a.zip",
                               {"RelayDesk.exe": b"<html>rate limited</html>"})
            payload = au.extract_payload(archive, Path(tmp) / "out")
            with self.assertRaises(au.UpdateError) as caught:
                au.validate_payload(payload)
            self.assertEqual(caught.exception.code, au.EXIT_INSTALL)


class SwapTests(unittest.TestCase):
    def test_swap_keeps_the_old_binary_as_a_prefixed_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "RelayDesk.exe"
            current.write_bytes(b"MZ-old")
            new = Path(tmp) / "new.exe"
            new.write_bytes(b"MZ-new")
            backup = au.swap_exe(current, new, stamp="20260101-000000")
            self.assertEqual(current.read_bytes(), b"MZ-new")
            self.assertEqual(backup.name, "RelayDesk.exe.prev-20260101-000000")
            self.assertEqual(backup.read_bytes(), b"MZ-old")

    def test_failed_copy_puts_the_old_binary_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "RelayDesk.exe"
            current.write_bytes(b"MZ-old")
            with mock.patch("flixcli.autoupdate.shutil.copy2", side_effect=OSError("locked")):
                with self.assertRaises(OSError):
                    au.swap_exe(current, Path(tmp) / "missing.exe", stamp="s")
            self.assertEqual(current.read_bytes(), b"MZ-old")

    def test_restore_prefers_the_target_name_not_the_backup_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "RelayDesk.exe"
            backup = Path(tmp) / "RelayDesk.exe.prev-stamp"
            target.write_bytes(b"MZ-new")
            backup.write_bytes(b"MZ-old")
            au._restore([backup], target)
            self.assertEqual(target.read_bytes(), b"MZ-old")

    def test_prune_keeps_the_newest_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            import os
            now = time.time()
            made = []
            for index in range(5):
                path = folder / f"RelayDesk.exe.prev-2026010{index}"
                path.write_bytes(b"x")
                # Explicit mtimes: writes within one clock tick would otherwise
                # tie and make "newest" depend on directory order.
                os.utime(path, (now - 100 * (4 - index),) * 2)
                made.append(path)
            au.prune_backups(folder, keep=2)
            self.assertEqual(sorted(p.name for p in made if p.exists()),
                             ["RelayDesk.exe.prev-20260103", "RelayDesk.exe.prev-20260104"])


class StateTests(unittest.TestCase):
    def test_record_survives_a_roundtrip_keyed_by_exe_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "update_state.json"
            exe = Path(tmp) / "RelayDesk.exe"
            exe.write_bytes(FAKE_EXE)
            state = au.remember_install(au.load_state(state_path), exe, tag="v2.2.1",
                                        asset="a.zip", sha256=sha(b"z"),
                                        exe_sha256=sha(FAKE_EXE), source="u")
            au.save_state(state, state_path)
            record = au.installed_record(au.load_state(state_path), exe)
            self.assertEqual(record["tag"], "v2.2.1")

    def test_corrupt_state_file_is_ignored_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "update_state.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(au.load_state(path), {})


class InstallFlowTests(unittest.TestCase):
    """Whole pipeline with the network stubbed and the archive pre-downloaded."""

    def _run(self, tmp: Path, *, tag: str, new_blob: bytes, record_tag: str | None):
        exe = tmp / "RelayDesk.exe"
        exe.write_bytes(FAKE_EXE)
        downloads = tmp / "dl"
        downloads.mkdir()
        asset_name = f"RelayDesk-{tag}-win64.zip"
        archive = make_zip(downloads / asset_name, {"RelayDesk.exe": new_blob})
        digest = au.sha256_file(archive)
        rel = release(tag, asset_name, digest, size=archive.stat().st_size)
        state_path = tmp / "state.json"
        if record_tag:
            state = au.remember_install({}, exe, tag=record_tag, asset="old.zip",
                                        sha256=sha(b"old"), exe_sha256=sha(FAKE_EXE),
                                        source="u")
            au.save_state(state, state_path)
        with mock.patch.object(au, "fetch_releases", return_value=[rel]), \
                mock.patch.object(au, "list_instances", return_value=[]):
            return au.run_update(exe=str(exe), repo="o/r", state_path=state_path,
                                 log_path=tmp / "update.log",
                                 downloads_dir=downloads, wait_s=1.0)

    def test_new_release_replaces_the_exe_and_records_the_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = self._run(tmp_path, tag="v9.9.9", new_blob=FAKE_EXE_V2,
                               record_tag="v1.0.0")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["status"], "installed")
            self.assertEqual((tmp_path / "RelayDesk.exe").read_bytes(), FAKE_EXE_V2)
            self.assertTrue(Path(result["backups"][0]).exists())
            record = au.installed_record(au.load_state(tmp_path / "state.json"),
                                         tmp_path / "RelayDesk.exe")
            self.assertEqual(record["tag"], "v9.9.9")

    def test_already_current_makes_no_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = self._run(tmp_path, tag="v2.2.1", new_blob=FAKE_EXE_V2,
                               record_tag="v2.2.1")
            self.assertEqual(result["status"], "current")
            self.assertEqual((tmp_path / "RelayDesk.exe").read_bytes(), FAKE_EXE)

    def test_check_only_reports_and_installs_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            exe = tmp_path / "RelayDesk.exe"
            exe.write_bytes(FAKE_EXE)
            state_path = tmp_path / "state.json"
            au.save_state(au.remember_install({}, exe, tag="v2.2.1", asset="o.zip",
                                              sha256="0", exe_sha256="0", source="u"),
                          state_path)
            archive = make_zip(tmp_path / "RelayDesk-v3.0.0-win64.zip",
                               {"RelayDesk.exe": FAKE_EXE_V2})
            rel = release("v3.0.0", "RelayDesk-v3.0.0-win64.zip",
                          au.sha256_file(archive))
            with mock.patch.object(au, "fetch_releases", return_value=[rel]):
                result = au.run_update(exe=str(exe), check_only=True,
                                       state_path=state_path,
                                       log_path=tmp_path / "u.log")
            self.assertEqual(result["status"], "update-available")
            self.assertEqual(result["planned"]["wouldInstall"], "v3.0.0")
            self.assertEqual(exe.read_bytes(), FAKE_EXE)

    def test_failed_restart_rolls_the_old_binary_back(self):
        """A new build that never binds its port must not leave the user with
        an exe that does not launch."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            exe = tmp_path / "RelayDesk.exe"
            exe.write_bytes(FAKE_EXE)
            downloads = tmp_path / "dl"
            downloads.mkdir()
            asset_name = "RelayDesk-v9.9.9-win64.zip"
            archive = make_zip(downloads / asset_name, {"RelayDesk.exe": FAKE_EXE_V2})
            rel = release("v9.9.9", asset_name, au.sha256_file(archive),
                          size=archive.stat().st_size)
            running = [{"pid": 4242, "exe": str(exe), "args": "serve --api-port 59999"}]
            with mock.patch.object(au, "fetch_releases", return_value=[rel]), \
                    mock.patch.object(au, "list_instances", return_value=running), \
                    mock.patch.object(au, "stop_instances", return_value=[4242]), \
                    mock.patch.object(au, "start_instance", return_value=None):
                result = au.run_update(exe=str(exe), state_path=tmp_path / "s.json",
                                       log_path=tmp_path / "u.log",
                                       downloads_dir=downloads, wait_s=1.0)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "unhealthy-rollback")
            self.assertEqual(exe.read_bytes(), FAKE_EXE)

    def test_checksum_mismatch_aborts_before_touching_the_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            exe = tmp_path / "RelayDesk.exe"
            exe.write_bytes(FAKE_EXE)
            downloads = tmp_path / "dl"
            downloads.mkdir()
            archive = make_zip(downloads / "RelayDesk-v9.9.9-win64.zip",
                               {"RelayDesk.exe": FAKE_EXE_V2})
            rel = release("v9.9.9", "RelayDesk-v9.9.9-win64.zip",
                          sha(b"something-else"), size=archive.stat().st_size)
            with mock.patch.object(au, "fetch_releases", return_value=[rel]), \
                    mock.patch.object(au, "list_instances", return_value=[]):
                with self.assertRaises(au.UpdateError) as caught:
                    au.run_update(exe=str(exe), state_path=tmp_path / "s.json",
                                  log_path=tmp_path / "u.log",
                                  downloads_dir=downloads, wait_s=1.0)
            self.assertEqual(caught.exception.kind, "checksum")
            self.assertEqual(exe.read_bytes(), FAKE_EXE)


class DryRunTests(unittest.TestCase):
    """`--dry-run` is the rehearsal: it must reach the same verdict as a real
    run and only then stop, because its whole value is catching a bad asset
    before the swap, not skipping the download."""

    def _run(self, tmp: Path, *, digest: str | None, force: bool = True,
             record_tag: str | None = "v9.9.9"):
        exe = tmp / "RelayDesk.exe"
        exe.write_bytes(FAKE_EXE)
        downloads = tmp / "dl"
        downloads.mkdir()
        asset_name = "RelayDesk-v9.9.9-win64.zip"
        archive = make_zip(downloads / asset_name, {"RelayDesk.exe": FAKE_EXE_V2})
        rel = release("v9.9.9", asset_name, digest or au.sha256_file(archive),
                      size=archive.stat().st_size)
        state_path = tmp / "state.json"
        if record_tag:
            au.save_state(au.remember_install({}, exe, tag=record_tag,
                                              asset=asset_name,
                                              sha256=sha(b"x"),
                                              exe_sha256=sha(FAKE_EXE),
                                              source="u"), state_path)
        with mock.patch.object(au, "fetch_releases", return_value=[rel]), \
                mock.patch.object(au, "list_instances", return_value=[]), \
                mock.patch.object(au, "stop_instances", side_effect=AssertionError), \
                mock.patch.object(au, "start_instance", side_effect=AssertionError):
            result = au.run_update(exe=str(exe), state_path=state_path,
                                   log_path=tmp / "u.log", downloads_dir=downloads,
                                   dry_run=True, force=force, wait_s=1.0)
        return result, exe

    def test_dry_run_downloads_verifies_and_installs_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, exe = self._run(Path(tmp), digest=None)
            self.assertEqual(result["status"], "dry-run")
            self.assertTrue(result["verified"])
            self.assertEqual(result["planned"]["wouldInstall"], "v9.9.9")
            self.assertEqual(len(result["sha256"]), 64)
            self.assertEqual(exe.read_bytes(), FAKE_EXE)
            self.assertEqual(list(Path(tmp).glob("RelayDesk.exe.prev-*")), [])

    def test_dry_run_rejects_a_bad_digest_instead_of_reporting_a_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(au.UpdateError) as caught:
                self._run(Path(tmp), digest=sha(b"something-else"))
            self.assertEqual(caught.exception.kind, "checksum")
            self.assertEqual((Path(tmp) / "RelayDesk.exe").read_bytes(), FAKE_EXE)

    def test_check_only_still_skips_the_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            exe = tmp_path / "RelayDesk.exe"
            exe.write_bytes(FAKE_EXE)
            rel = release("v9.9.9", "RelayDesk-v9.9.9-win64.zip", sha(b"z"))
            with mock.patch.object(au, "fetch_releases", return_value=[rel]), \
                    mock.patch.object(au, "download_asset",
                                      side_effect=AssertionError):
                result = au.run_update(exe=str(exe), check_only=True,
                                       state_path=tmp_path / "s.json",
                                       log_path=tmp_path / "u.log")
            self.assertEqual(result["status"], "update-available")
            self.assertNotIn("sha256", result)


class LockTests(unittest.TestCase):
    def test_second_updater_is_refused_while_the_first_holds_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "update.lock"
            with au.UpdateLock(path):
                with self.assertRaises(au.UpdateError) as caught:
                    with au.UpdateLock(path):
                        pass
            self.assertEqual(caught.exception.kind, "busy")
            self.assertFalse(path.exists())

    def test_stale_lock_from_a_crashed_run_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "update.lock"
            path.write_text("999999 crashed", encoding="utf-8")
            ancient = 1_600_000_000
            import os
            os.utime(path, (ancient, ancient))
            with au.UpdateLock(path, stale_after=60):
                self.assertTrue(path.exists())


class CliTests(unittest.TestCase):
    """`--state-dir` is always passed: these tests must not append to the real
    update.log of whoever runs the suite."""

    def _main(self, argv):
        from flixcli.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            return main([*argv, "--state-dir", tmp])

    def test_update_subcommand_is_registered(self):
        """`flix update --help` must resolve; a broken help string is the first
        thing an argparse refactor silently breaks."""
        import contextlib
        import io

        from flixcli.cli import main

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with self.assertRaises(SystemExit) as caught:
                main(["update", "--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("--check", buffer.getvalue())

    def test_network_failure_maps_to_exit_code_3(self):
        with mock.patch("flixcli.autoupdate.run_update",
                        side_effect=au.UpdateError("network", "offline", au.EXIT_NETWORK)), \
                mock.patch("sys.stdout"):
            self.assertEqual(self._main(["update", "--json"]), au.EXIT_NETWORK)

    def test_update_available_maps_to_exit_code_10_under_check(self):
        payload = {"ok": True, "status": "update-available", "latest": "v9"}
        with mock.patch("flixcli.autoupdate.run_update", return_value=payload), \
                mock.patch("sys.stdout"):
            self.assertEqual(self._main(["update", "--check"]), au.EXIT_UPDATE_AVAILABLE)
            self.assertEqual(self._main(["update"]), 0)


class TargetTests(unittest.TestCase):
    def test_missing_explicit_exe_is_a_local_env_error(self):
        with self.assertRaises(au.UpdateError) as caught:
            au.detect_target(explicit="/nonexistent/RelayDesk.exe")
        self.assertEqual(caught.exception.code, au.EXIT_ENV)


if __name__ == "__main__":
    unittest.main()
