"""Isolated supply-chain and extraction checks; no downloads or app changes."""
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "package_dot_runtime.py"
SPEC = importlib.util.spec_from_file_location("package_dot_runtime", MODULE)
packaging = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packaging)


class PackagingSafetyTests(unittest.TestCase):
    def test_tampered_cached_download_never_extracts_or_refetches(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp)
            (cache / "archive.tar.gz").write_bytes(b"modified")
            with patch.object(packaging.urllib.request, "urlopen") as network:
                with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                    packaging.fetch({"filename": "archive.tar.gz", "sha256": "0" * 64}, cache)
                network.assert_not_called()

    def test_parent_traversal_is_rejected_before_tar_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "bad.tar.gz"
            with tarfile.open(archive, "w:gz") as source:
                entry = tarfile.TarInfo("python/../../escaped")
                entry.size = 1
                source.addfile(entry, io.BytesIO(b"x"))
            with patch.object(packaging, "run") as tar:
                with self.assertRaisesRegex(ValueError, "Unsafe runtime archive path"):
                    packaging.extract_runtime(archive, Path(temp) / "output")
                tar.assert_not_called()

    def test_symlink_escape_is_rejected_before_tar_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "bad.tar.gz"
            with tarfile.open(archive, "w:gz") as source:
                entry = tarfile.TarInfo("python/bin/python3")
                entry.type = tarfile.SYMTYPE
                entry.linkname = "/usr/bin/python3"
                source.addfile(entry)
            with patch.object(packaging, "run") as tar:
                with self.assertRaisesRegex(ValueError, "Unsafe runtime archive symlink"):
                    packaging.extract_runtime(archive, Path(temp) / "output")
                tar.assert_not_called()

    def test_all_checked_in_notices_match_pinned_upstream_bytes(self):
        runtime = json.loads(packaging.LOCK.read_text())
        self.assertGreaterEqual(len(runtime["noticeFiles"]), 20)
        for name, expected in runtime["noticeFiles"].items():
            self.assertEqual(packaging.digest(packaging.LOCK.parent / name), expected, name)


if __name__ == "__main__":
    unittest.main()
