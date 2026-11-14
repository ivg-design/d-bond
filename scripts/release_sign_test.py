"""Notarization retry boundaries without Apple requests or signing keys."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import release_sign


class NotarizationBoundaryTests(unittest.TestCase):
    def test_changed_artifact_never_authenticates_or_uploads(self):
        with patch.object(release_sign, "verify", return_value={"bundleDigest": "changed"}), \
             patch.object(release_sign, "preflight") as authentication, \
             patch.object(release_sign, "command") as apple:
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                release_sign.notarize(Path("app"), "approved", Path("unused"))
        authentication.assert_not_called()
        apple.assert_not_called()

    def test_unknown_submission_requires_reconciliation_before_another_upload(self):
        with tempfile.TemporaryDirectory() as temp:
            receipt = Path(temp) / "approved.json"
            receipt.write_text(json.dumps({"status": "submission-uncertain"}))
            with patch.object(release_sign, "verify", return_value={"bundleDigest": "approved"}), \
                 patch.object(release_sign, "preflight"), patch.object(release_sign, "command") as apple:
                with self.assertRaisesRegex(RuntimeError, "unknown outcome"):
                    release_sign.notarize(Path("app"), "approved", Path(temp))
            apple.assert_not_called()

    def test_existing_submission_resumes_without_upload(self):
        calls = []
        def apple(args, **kwargs):
            calls.append([str(value) for value in args])
            if "wait" in args:
                return subprocess.CompletedProcess(args, 0, '{"status":"Accepted"}', "")
            if "log" in args:
                return subprocess.CompletedProcess(args, 0, '{"issues":[]}', "")
            return subprocess.CompletedProcess(args, 0, "", "")
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "approved.json").write_text(json.dumps({"id": "existing-id", "status": "In Progress"}))
            with patch.object(release_sign, "verify", return_value={"bundleDigest": "approved"}), \
                 patch.object(release_sign, "preflight"), patch.object(release_sign, "profile", return_value="test"), \
                 patch.object(release_sign, "fingerprint", return_value="approved"), \
                 patch.object(release_sign, "command", side_effect=apple):
                result = release_sign.notarize(Path("app"), "approved", Path(temp))
            self.assertEqual(result["status"], "Accepted")
            self.assertEqual(result["notaryWarnings"], 0)
            self.assertFalse(any("submit" in args for args in calls))
            self.assertEqual(sum("wait" in args for args in calls), 1)

    def test_bundle_digest_detects_code_and_symlink_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp)
            (app / "code").write_bytes(b"one")
            original = release_sign.fingerprint(app)
            (app / "code").write_bytes(b"two")
            self.assertNotEqual(release_sign.fingerprint(app), original)
            (app / "alias").symlink_to("code")
            linked = release_sign.fingerprint(app)
            (app / "alias").unlink()
            (app / "alias").symlink_to("other")
            self.assertNotEqual(release_sign.fingerprint(app), linked)


if __name__ == "__main__":
    unittest.main()
