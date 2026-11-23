"""로컬 토큰 파일 만들기·확장 폴더에 쓰기 (local_token.py, updater.sync_bundled_extension)"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import local_token  # noqa: E402
import updater  # noqa: E402

TOKEN = "12" * 32


def make_ext(folder: Path, version: str = "0.1.0") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    return folder


class TokenFileTests(unittest.TestCase):
    def test_creates_then_reuses(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "local-token.txt"
            first = local_token.load_or_create_token(path)
            self.assertTrue(local_token.is_valid_token(first))
            self.assertEqual(path.read_text(encoding="utf-8"), first)
            self.assertEqual(local_token.load_or_create_token(path), first)

    def test_regenerates_broken_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "local-token.txt"
            path.write_text("not-a-token", encoding="utf-8")
            token = local_token.load_or_create_token(path)
            self.assertTrue(local_token.is_valid_token(token))
            self.assertEqual(path.read_text(encoding="utf-8"), token)

    def test_tokens_are_random(self):
        self.assertNotEqual(local_token.new_token(), local_token.new_token())

    def test_token_matches(self):
        self.assertTrue(local_token.token_matches(TOKEN, TOKEN))
        self.assertFalse(local_token.token_matches(TOKEN, "34" * 32))
        self.assertFalse(local_token.token_matches(TOKEN, None))
        self.assertFalse(local_token.token_matches(None, TOKEN))
        self.assertFalse(local_token.token_matches(TOKEN, "토큰"))


class ExtensionTokenFileTests(unittest.TestCase):
    def test_writes_json_for_extension(self):
        with tempfile.TemporaryDirectory() as d:
            ext = make_ext(Path(d) / "ext")
            self.assertTrue(local_token.write_extension_token(ext, TOKEN))
            data = json.loads((ext / "local-token.json").read_text(encoding="utf-8"))
            self.assertEqual(data, {"token": TOKEN, "header": "X-AIW-Token"})
            mtime = (ext / "local-token.json").stat().st_mtime_ns
            self.assertTrue(local_token.write_extension_token(ext, TOKEN))  # 같으면 그대로
            self.assertEqual((ext / "local-token.json").stat().st_mtime_ns, mtime)

    def test_skips_non_extension_folder_and_bad_token(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(local_token.write_extension_token(Path(d), TOKEN))
            self.assertFalse((Path(d) / "local-token.json").exists())
            ext = make_ext(Path(d) / "ext")
            self.assertFalse(local_token.write_extension_token(ext, "short"))

    def test_sync_does_not_copy_bundled_token(self):
        # 개발 PC에서 만든 exe에 개발용 토큰 파일이 들어 있어도 복사하지 않는다
        with tempfile.TemporaryDirectory() as d:
            src, dst = make_ext(Path(d) / "src", "0.2.0"), Path(d) / "dst"
            (src / "local-token.json").write_text("{}", encoding="utf-8")
            (src / "background.js").write_text("x", encoding="utf-8")
            self.assertTrue(updater.sync_extension(src, dst))
            self.assertTrue((dst / "background.js").exists())
            self.assertFalse((dst / "local-token.json").exists())

    def test_sync_bundled_writes_token_after_copy(self):
        with tempfile.TemporaryDirectory() as d:
            src, dst = make_ext(Path(d) / "src", "0.3.0"), make_ext(Path(d) / "dst", "0.2.0")
            (dst / "local-token.json").write_text("old", encoding="utf-8")
            with mock.patch.object(updater, "is_frozen", return_value=True), \
                 mock.patch.object(updater, "bundled_extension_dir", return_value=src), \
                 mock.patch.object(updater, "chrome_extension_dir", return_value=dst):
                self.assertTrue(updater.sync_bundled_extension(TOKEN))
            self.assertEqual(updater.read_manifest_version(dst), "0.3.0")
            data = json.loads((dst / "local-token.json").read_text(encoding="utf-8"))
            self.assertEqual(data["token"], TOKEN)

    def test_source_run_writes_token_only(self):
        with tempfile.TemporaryDirectory() as d:
            ext = make_ext(Path(d) / "extension")
            with mock.patch.object(updater, "is_frozen", return_value=False), \
                 mock.patch.object(updater, "chrome_extension_dir", return_value=ext):
                self.assertFalse(updater.sync_bundled_extension(TOKEN))  # 복사는 없음
            data = json.loads((ext / "local-token.json").read_text(encoding="utf-8"))
            self.assertEqual(data["token"], TOKEN)


if __name__ == "__main__":
    unittest.main()
