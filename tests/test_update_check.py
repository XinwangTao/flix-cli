"""启动时的版本检查：结果结构与 /healthz 透出。"""
from __future__ import annotations

import contextlib
import io
import unittest
import urllib.error
from unittest import mock

from flixcli import autoupdate
from flixcli.webapp import health_payload


class CheckUpdateTests(unittest.TestCase):
    def _release(self, tag):
        return autoupdate.Release(tag=tag, name=tag, prerelease=False,
                                  draft=False, url="https://example/" + tag)

    def test_reports_newer_release(self):
        with mock.patch.object(autoupdate, "fetch_releases", return_value=[self._release("v9.9.9")]):
            info = autoupdate.check_update(current="2.2.1")
        self.assertTrue(info["ok"])
        self.assertTrue(info["available"])
        self.assertEqual(info["latest"], "9.9.9")
        self.assertEqual(info["current"], "2.2.1")

    def test_same_or_older_is_not_available(self):
        with mock.patch.object(autoupdate, "fetch_releases", return_value=[self._release("v2.2.1")]):
            self.assertFalse(autoupdate.check_update(current="2.2.1")["available"])
        with mock.patch.object(autoupdate, "fetch_releases", return_value=[self._release("v2.0.0")]):
            self.assertFalse(autoupdate.check_update(current="2.2.1")["available"])

    def test_network_error_degrades_quietly(self):
        err = autoupdate.UpdateError("network", "timeout")
        with mock.patch.object(autoupdate, "fetch_releases", side_effect=err):
            info = autoupdate.check_update(current="2.2.1")
        self.assertFalse(info["ok"])
        self.assertEqual(info["error"], "network")
        self.assertFalse(info.get("available", False))


class HealthPayloadUpdateTests(unittest.TestCase):
    def test_health_exposes_update_state(self):
        ident = {"alias": "board", "fingerprint": "abc", "version": 1}
        info = {"ok": True, "current": "2.2.1", "latest": "2.3.0", "available": True}
        payload = health_payload(ident, 0.0, 5.0, 1, 8891, update=info)
        self.assertEqual(payload["update"]["latest"], "2.3.0")

    def test_health_without_update_is_null(self):
        ident = {"alias": "board", "fingerprint": "abc", "version": 1}
        self.assertIsNone(health_payload(ident, 0.0, 5.0, 1, 8891)["update"])


class FetchReleasesPageTests(unittest.TestCase):
    """A page of 10 could hide the latest stable behind newer prereleases."""

    def _url(self, **kwargs) -> str:
        calls = []

        def fake_open(url, *, token=None, timeout=20.0):
            calls.append(url)
            return contextlib.nullcontext(io.BytesIO(b"[]"))

        with mock.patch.object(autoupdate, "_open", side_effect=fake_open):
            self.assertEqual(autoupdate.fetch_releases("o/r", **kwargs), [])
        return calls[0]

    def test_default_page_is_100(self):
        self.assertTrue(self._url().endswith("?per_page=100"))

    def test_explicit_page_is_kept(self):
        self.assertTrue(self._url(per_page=5).endswith("?per_page=5"))

    def test_page_is_clamped_to_github_max(self):
        self.assertTrue(self._url(per_page=1000).endswith("?per_page=100"))


class FetchReleases404Tests(unittest.TestCase):
    """A proxy 404 on the direct API must not skip the remaining mirrors."""

    @staticmethod
    def _404(url):
        return urllib.error.HTTPError(url, 404, "Not Found", None, None)

    def test_direct_404_falls_back_to_the_mirror(self):
        calls = []

        def fake_open(url, *, token=None, timeout=20.0):
            calls.append(url)
            if len(calls) == 1:
                raise self._404(url)
            return contextlib.nullcontext(
                io.BytesIO(b'[{"tag_name": "v2.3.0"}]'))

        with mock.patch.object(autoupdate, "_open", side_effect=fake_open):
            releases = autoupdate.fetch_releases(
                "o/r", mirrors=("https://mirror/",))
        self.assertEqual([r.tag for r in releases], ["v2.3.0"])
        self.assertEqual(len(calls), 2)

    def test_all_404_still_reports_no_releases(self):
        def fake_open(url, *, token=None, timeout=20.0):
            raise self._404(url)

        with mock.patch.object(autoupdate, "_open", side_effect=fake_open):
            with self.assertRaises(autoupdate.UpdateError) as caught:
                autoupdate.fetch_releases(
                    "o/r", mirrors=("https://m1/", "https://m2/"))
        self.assertEqual(caught.exception.kind, "no-releases")

    def test_mixed_404_and_network_error_reports_network(self):
        calls = []

        def fake_open(url, *, token=None, timeout=20.0):
            calls.append(url)
            if len(calls) == 1:
                raise self._404(url)
            raise urllib.error.URLError("down")

        with mock.patch.object(autoupdate, "_open", side_effect=fake_open):
            with self.assertRaises(autoupdate.UpdateError) as caught:
                autoupdate.fetch_releases("o/r", mirrors=("https://m1/",))
        self.assertEqual(caught.exception.kind, "network")


class FetchReleasesPayloadTests(unittest.TestCase):
    """A 200 response with an error object must not abort the mirror loop."""

    def test_non_list_payload_falls_back_to_the_next_mirror(self):
        payloads = [b'{"message": "rate limited"}', b'[{"tag_name": "v2.3.0"}]']
        calls = []

        def fake_open(url, *, token=None, timeout=20.0):
            calls.append(url)
            return contextlib.nullcontext(io.BytesIO(payloads[len(calls) - 1]))

        with mock.patch.object(autoupdate, "_open", side_effect=fake_open):
            releases = autoupdate.fetch_releases(
                "o/r", mirrors=("https://mirror/",))
        self.assertEqual([r.tag for r in releases], ["v2.3.0"])
        self.assertEqual(len(calls), 2)

    def test_all_bad_payloads_raise_network(self):
        def fake_open(url, *, token=None, timeout=20.0):
            return contextlib.nullcontext(
                io.BytesIO(b'{"message": "rate limited"}'))

        with mock.patch.object(autoupdate, "_open", side_effect=fake_open):
            with self.assertRaises(autoupdate.UpdateError) as caught:
                autoupdate.fetch_releases(
                    "o/r", mirrors=("https://m1/", "https://m2/"))
        self.assertEqual(caught.exception.kind, "network")
        self.assertIn("unexpected releases payload", caught.exception.detail)

    def test_malformed_asset_entries_are_skipped(self):
        payload = (b'[{"tag_name": "v2.3.0", "assets": ['
                   b'{"name": "a.zip", "size": 1, "browser_download_url": "u"},'
                   b'"junk", {"name": "b.zip", "size": "not-a-number"}]}]')
        with mock.patch.object(
                autoupdate, "_open",
                return_value=contextlib.nullcontext(io.BytesIO(payload))):
            releases = autoupdate.fetch_releases("o/r")
        self.assertEqual([a.name for a in releases[0].assets], ["a.zip", "b.zip"])
        self.assertEqual(releases[0].assets[1].size, 0)


class CheckUpdateTokenTests(unittest.TestCase):
    """The daemon's startup check must honour GITHUB_TOKEN too."""

    def _token_used(self, **kwargs):
        rel = autoupdate.Release("v9.9.9", "v9.9.9", False, False, "u")
        with mock.patch.object(autoupdate, "fetch_releases",
                               return_value=[rel]) as fetch:
            autoupdate.check_update(current="2.2.1", **kwargs)
        return fetch.call_args.kwargs.get("token")

    def test_explicit_token_is_forwarded(self):
        self.assertEqual(self._token_used(token="abc"), "abc")

    def test_environment_token_is_used(self):
        with mock.patch.dict(autoupdate.os.environ,
                             {"GITHUB_TOKEN": "env-token"}):
            self.assertEqual(self._token_used(), "env-token")

    def test_empty_environment_token_means_none(self):
        with mock.patch.dict(autoupdate.os.environ,
                             {"GITHUB_TOKEN": "", "FLIX_GITHUB_TOKEN": ""}):
            self.assertIsNone(self._token_used())


if __name__ == "__main__":
    unittest.main()
