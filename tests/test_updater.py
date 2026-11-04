"""Tests for app.updater: release asset choice, archive layout, safety checks.

Run with:  python -m unittest discover tests
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from app import updater

REPO = "brianbcyangmusic-pixel/playlist-downloader"
BASE = f"https://github.com/{REPO}/releases/download/v9.9.9/"
ASSETS = [
    {"name": "PlaylistDownloader-macOS.zip", "browser_download_url": BASE + "PlaylistDownloader-macOS.zip"},
    {"name": "PlaylistDownloader-Windows-x64.zip", "browser_download_url": BASE + "PlaylistDownloader-Windows-x64.zip"},
]


class PickAssetTests(unittest.TestCase):
    def test_macos_picks_the_macos_zip(self) -> None:
        with mock.patch.object(updater.sys, "platform", "darwin"):
            self.assertEqual(updater.pick_asset(ASSETS)["name"], "PlaylistDownloader-macOS.zip")

    def test_windows_picks_the_windows_zip(self) -> None:
        with mock.patch.object(updater.sys, "platform", "win32"):
            self.assertEqual(updater.pick_asset(ASSETS)["name"], "PlaylistDownloader-Windows-x64.zip")

    def test_linux_has_no_asset(self) -> None:
        with mock.patch.object(updater.sys, "platform", "linux"):
            self.assertIsNone(updater.pick_asset(ASSETS))


class VersionTests(unittest.TestCase):
    def test_newer_versions_compare_numerically(self) -> None:
        self.assertTrue(updater.is_newer("v1.10.0", "1.9.3"))
        self.assertFalse(updater.is_newer("v1.6.1", "1.6.1"))
        self.assertFalse(updater.is_newer("garbage", "1.0.0"))


class TrustedUrlTests(unittest.TestCase):
    def test_accepts_only_this_projects_release_downloads(self) -> None:
        cfg = {"update_repo": REPO}
        self.assertTrue(updater.trusted_asset_url(ASSETS[0]["browser_download_url"], cfg))
        self.assertFalse(updater.trusted_asset_url("https://github.com/someone/else/releases/download/x.zip", cfg))
        self.assertFalse(updater.trusted_asset_url("http://github.com/" + REPO + "/releases/download/x.zip", cfg))
        self.assertFalse(updater.trusted_asset_url("https://evil.example/x.zip", cfg))


class FindNewAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def test_windows_layout_at_zip_root(self) -> None:
        (self.tmp / "backend").mkdir()
        (self.tmp / "PlaylistDownloader.exe").write_bytes(b"x")
        (self.tmp / "backend" / "PlaylistDownloaderBackend.exe").write_bytes(b"x")
        with mock.patch.object(updater.sys, "platform", "win32"):
            self.assertEqual(updater._find_new_app(self.tmp), self.tmp)

    def test_windows_layout_missing_backend_is_rejected(self) -> None:
        (self.tmp / "PlaylistDownloader.exe").write_bytes(b"x")
        with mock.patch.object(updater.sys, "platform", "win32"):
            self.assertIsNone(updater._find_new_app(self.tmp))

    def test_macos_bundle_needs_an_executable_launcher(self) -> None:
        launcher = self.tmp / "Playlist Downloader.app" / updater.MAC_LAUNCHER
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(b"x")
        with mock.patch.object(updater.sys, "platform", "darwin"):
            self.assertIsNone(updater._find_new_app(self.tmp))
            launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
            self.assertEqual(updater._find_new_app(self.tmp), self.tmp / "Playlist Downloader.app")


class UnpackTests(unittest.TestCase):
    def test_zip_slip_is_refused(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        archive = tmp / "evil.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("../outside.txt", "nope")
        with mock.patch.object(updater.sys, "platform", "win32"):
            with self.assertRaises(ValueError):
                updater._unpack(archive, tmp / "out")
        self.assertFalse((tmp / "outside.txt").exists())

    @unittest.skipUnless(sys.platform == "darwin", "ditto is macOS-only")
    def test_macos_unpack_keeps_executable_bits(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        src = tmp / "Playlist Downloader.app" / updater.MAC_LAUNCHER
        src.parent.mkdir(parents=True)
        src.write_bytes(b"#!/bin/sh\n")
        src.chmod(0o755)
        archive = tmp / "app.zip"
        os.system(f'cd "{tmp}" && /usr/bin/ditto -c -k --keepParent "Playlist Downloader.app" app.zip')
        updater._unpack(archive, tmp / "out")
        self.assertTrue(os.access(tmp / "out" / "Playlist Downloader.app" / updater.MAC_LAUNCHER, os.X_OK))


class UnsupportedReasonTests(unittest.TestCase):
    def test_translocated_app_is_told_to_move_to_applications(self) -> None:
        bundle = Path("/private/var/folders/x/T/AppTranslocation/ABC/d/Playlist Downloader.app")
        with mock.patch.object(updater, "install_root", return_value=bundle):
            self.assertIn("Applications", updater.unsupported_reason() or "")
            self.assertFalse(updater.can_self_update())

    def test_normal_install_can_update(self) -> None:
        bundle = Path(tempfile.mkdtemp()) / "Playlist Downloader.app"
        with mock.patch.object(updater, "install_root", return_value=bundle):
            self.assertIsNone(updater.unsupported_reason())


class InstallTests(unittest.TestCase):
    def test_source_checkout_refuses_to_self_update(self) -> None:
        with mock.patch.object(updater, "install_root", return_value=None):
            result = updater.install(ASSETS[0]["browser_download_url"], {})
        self.assertFalse(result["ok"])

    def test_windows_helper_script_is_ascii(self) -> None:
        # Windows PowerShell 5.1 reads a BOM-less script as ANSI.
        self.assertTrue(updater._WIN_HELPER.isascii())


if __name__ == "__main__":
    unittest.main()
