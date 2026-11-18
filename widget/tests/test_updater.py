import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import updater  # noqa: E402
from version import __version__  # noqa: E402


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, headers=None):
        super().__init__(data)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def release_json(tag="v9.9.9", data=b"new exe", digest=True):
    asset = {
        "name": "AIUsageWidget.exe",
        "browser_download_url": "https://example.com/AIUsageWidget.exe",
        "size": len(data),
    }
    if digest:
        asset["digest"] = "sha256:" + hashlib.sha256(data).hexdigest()
    return {
        "tag_name": tag,
        "body": "변경 사항",
        "html_url": "https://github.com/x/y/releases/tag/" + tag,
        "assets": [{"name": "ai-usage-extension.zip"}, asset],
    }


class VersionTests(unittest.TestCase):
    def test_parse_and_compare(self):
        self.assertEqual(updater.parse_version("v0.10.2"), (0, 10, 2))
        self.assertEqual(updater.parse_version("1.2"), (1, 2, 0))
        self.assertEqual(updater.parse_version("1.2.3-beta"), (1, 2, 3))
        self.assertTrue(updater.is_newer("0.10.0", "0.9.9"))
        self.assertFalse(updater.is_newer("v0.2.0", "0.2.0"))
        self.assertFalse(updater.is_newer("0.1.9", "0.2.0"))

    def test_versions_in_sync(self):
        repo = ROOT.parent
        manifest = json.loads((repo / "extension" / "manifest.json").read_text(encoding="utf-8"))
        package = json.loads((repo / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], __version__)
        self.assertEqual(package["version"], __version__)


class ReleaseTests(unittest.TestCase):
    def test_parse_release(self):
        info = updater.parse_release(release_json())
        self.assertEqual(info.version, "9.9.9")
        self.assertEqual(info.exe_size, 7)
        self.assertEqual(len(info.exe_sha256), 64)
        self.assertTrue(info.exe_url.endswith("AIUsageWidget.exe"))

    def test_parse_release_without_exe(self):
        data = release_json()
        data["assets"] = []
        self.assertIsNone(updater.parse_release(data).exe_url)
        with self.assertRaises(updater.UpdateError):
            updater.parse_release({})

    def test_fetch_latest(self):
        body = json.dumps(release_json()).encode()
        info = updater.fetch_latest(get=lambda url, t: FakeResponse(body))
        self.assertEqual(info.tag, "v9.9.9")

    def test_fetch_latest_network_error(self):
        def boom(url, t):
            raise OSError("offline")

        with self.assertRaises(updater.UpdateError):
            updater.fetch_latest(get=boom)


class DownloadTests(unittest.TestCase):
    def test_download_ok_with_progress(self):
        data = b"x" * 200_000
        info = updater.parse_release(release_json(data=data))
        seen = []
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "u" / "AIUsageWidget.exe"
            updater.download(info, dest, progress=seen.append, get=lambda u, t: FakeResponse(data))
            self.assertEqual(dest.read_bytes(), data)
            self.assertFalse(dest.with_suffix(".exe.part").exists())
        self.assertEqual(seen[-1], 100)

    def test_download_rejects_bad_hash(self):
        info = updater.parse_release(release_json(data=b"good!!!"))
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "AIUsageWidget.exe"
            with self.assertRaises(updater.UpdateError):
                updater.download(info, dest, get=lambda u, t: FakeResponse(b"evil!!!"))
            self.assertFalse(dest.exists())

    def test_download_rejects_bad_size(self):
        info = updater.parse_release(release_json(data=b"1234567", digest=False))
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(updater.UpdateError):
                updater.download(info, Path(d) / "a.exe", get=lambda u, t: FakeResponse(b"123"))


class ExtensionSyncTests(unittest.TestCase):
    def make_ext(self, folder: Path, version: str, extra_file: str):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "manifest.json").write_text(json.dumps({"version": version}), encoding="utf-8")
        (folder / extra_file).write_text("x", encoding="utf-8")

    def test_sync_copies_only_when_newer(self):
        with tempfile.TemporaryDirectory() as d:
            src, dst = Path(d) / "src", Path(d) / "dst"
            self.make_ext(src, "0.2.0", "new.js")
            self.assertTrue(updater.sync_extension(src, dst))  # 없으면 복사
            self.assertFalse(updater.sync_extension(src, dst))  # 같은 버전이면 그대로

            self.make_ext(dst, "0.2.0", "old_only.js")
            self.make_ext(src, "0.3.0", "new.js")
            self.assertTrue(updater.sync_extension(src, dst))
            self.assertEqual(updater.read_manifest_version(dst), "0.3.0")
            self.assertFalse((dst / "old_only.js").exists())  # 지워진 파일은 남지 않음


class FinishUpdateTests(unittest.TestCase):
    def test_replaces_target_and_restarts(self):
        with tempfile.TemporaryDirectory() as d:
            new, target = Path(d) / "new.exe", Path(d) / "AIUsageWidget.exe"
            new.write_bytes(b"v2")
            target.write_bytes(b"v1")
            with mock.patch.object(updater.subprocess, "Popen") as popen:
                self.assertTrue(updater.finish_update(target, new, sleep=lambda s: None))
            self.assertEqual(target.read_bytes(), b"v2")
            self.assertEqual(popen.call_args[0][0], [str(target)])

    def test_gives_up_and_restarts_old_version(self):
        with tempfile.TemporaryDirectory() as d:
            new, target = Path(d) / "new.exe", Path(d) / "AIUsageWidget.exe"
            new.write_bytes(b"v2")
            target.write_bytes(b"v1")
            with mock.patch.object(updater.shutil, "copy2", side_effect=PermissionError), \
                 mock.patch.object(updater.subprocess, "Popen") as popen:
                self.assertFalse(updater.finish_update(target, new, timeout=0, sleep=lambda s: None))
            self.assertEqual(target.read_bytes(), b"v1")
            popen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
