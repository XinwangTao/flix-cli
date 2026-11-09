"""Archive extraction for auto-update must stay inside its staging dir.

Release zips and the board's source tarball come from the network, and the
board service runs with ``FLIX_AUTO_UPDATE=1`` as root. A member named
``../x`` or an absolute path must never be written outside the disposable
work directory (zip-slip / tar traversal), and the staging directory must not
be a predictable path that a local user can pre-create.
"""
from __future__ import annotations

import contextlib
import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from flixcli import autoupdate as au

PE = b"MZ" + b"x" * 32


def tar_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class ZipContainmentTests(unittest.TestCase):
    def test_parent_member_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            archive = tmp / "evil.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("RelayDesk.exe", PE)
                zf.writestr("../escaped.txt", b"owned")
            with self.assertRaises(au.UpdateError) as caught:
                au.extract_payload(archive, tmp / "out")
            self.assertEqual(caught.exception.kind, "bad-archive")
            self.assertFalse((tmp / "escaped.txt").exists())

    def test_absolute_member_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            outside = (tmp / "escaped-abs.txt").as_posix()
            archive = tmp / "evil.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("RelayDesk.exe", PE)
                zf.writestr(outside, b"owned")
            with self.assertRaises(au.UpdateError):
                au.extract_payload(archive, tmp / "out")
            self.assertFalse((tmp / "escaped-abs.txt").exists())

    def test_normal_zip_still_extracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            archive = tmp / "ok.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("RelayDesk.exe", PE)
            payload = au.extract_payload(archive, tmp / "out")
            self.assertEqual(payload.kind, "exe")
            self.assertEqual(payload.path.read_bytes(), PE)


class TarContainmentTests(unittest.TestCase):
    def test_parent_member_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            archive = tmp / "evil.tar.gz"
            archive.write_bytes(tar_bytes({
                "pkg/pyproject.toml": b"[project]\n",
                "../escaped.txt": b"owned",
            }))
            with self.assertRaises(au.UpdateError) as caught:
                au._safe_extract_tar(archive, tmp / "out")
            self.assertEqual(caught.exception.kind, "bad-archive")
            self.assertFalse((tmp / "escaped.txt").exists())

    def test_absolute_member_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            archive = tmp / "evil.tar.gz"
            archive.write_bytes(tar_bytes({
                (tmp / "escaped-abs.txt").as_posix(): b"owned",
            }))
            with self.assertRaises(au.UpdateError):
                au._safe_extract_tar(archive, tmp / "out")
            self.assertFalse((tmp / "escaped-abs.txt").exists())

    def test_manual_member_check_also_rejects_traversal(self):
        """The fallback path must hold on interpreters without data_filter."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            archive = tmp / "evil.tar.gz"
            archive.write_bytes(tar_bytes({"../escaped.txt": b"owned"}))
            with tarfile.open(archive, "r:gz") as tar:
                with self.assertRaises(au.UpdateError):
                    au._check_tar_members(tar, (tmp / "out").resolve())
            self.assertFalse((tmp / "escaped.txt").exists())

    def test_normal_tarball_still_extracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            archive = tmp / "ok.tar.gz"
            archive.write_bytes(tar_bytes({
                "repo-abc/pyproject.toml": b"[project]\n",
                "repo-abc/src/app.py": b"x = 1\n",
            }))
            au._safe_extract_tar(archive, tmp / "out")
            self.assertEqual((tmp / "out/repo-abc/src/app.py").read_bytes(),
                             b"x = 1\n")


class SourceUpdateWorkdirTests(unittest.TestCase):
    def _install(self, parent: Path, blob: bytes) -> tuple[str, mock.Mock]:
        proc = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(au, "_open",
                               return_value=contextlib.nullcontext(io.BytesIO(blob))), \
                mock.patch.object(au, "_find_uv", return_value=None), \
                mock.patch.object(au.subprocess, "run", return_value=proc) as run:
            version = au.install_from_source("v9.9.9", workdir=parent)
        return version, run

    def test_existing_caller_files_survive_and_run_dir_is_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "work"
            parent.mkdir()
            keep = parent / "keep.txt"
            keep.write_text("keep", encoding="utf-8")
            blob = tar_bytes({"repo-abc/pyproject.toml": b"[project]\n"})
            version, run = self._install(parent, blob)
            self.assertEqual(version, "9.9.9")
            self.assertTrue(keep.exists(), "install must not wipe the caller dir")
            self.assertEqual(list(parent.iterdir()), [keep],
                             "per-run staging dir must be removed")
            source = Path(run.call_args_list[-1].args[0][-1])
            self.assertEqual(source.parent.parent, parent)
            self.assertTrue(source.parent.name.startswith("flixcli-update-"))

    def test_traversal_tarball_installs_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "work"
            blob = tar_bytes({"../escaped.txt": b"owned"})
            with self.assertRaises(au.UpdateError):
                self._install(parent, blob)
            self.assertFalse((Path(tmp) / "escaped.txt").exists())
            self.assertEqual(list(parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
