"""Notarization retry boundaries without Apple requests or signing keys."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile
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


class ArchivedSignatureTests(unittest.TestCase):
    def test_archive_macho_checks_outer_app_team(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "adapter.mcpb"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("server/launcher", b"\xcf\xfa\xed\xfe" + b"binary")
                bundle.writestr("manifest.json", "{}")
            with patch.object(release_sign, "signature") as signature:
                self.assertEqual(release_sign.verify_archive(archive, True, "expected-team"), 1)
                self.assertEqual(signature.call_args.args[1:], (True, "expected-team"))

    def test_unsigned_archive_binary_fails_release_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "adapter.mcpb"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("server/launcher", b"\xcf\xfa\xed\xfe" + b"unsigned")
            with patch.object(release_sign, "signature", side_effect=RuntimeError("not signed")):
                with self.assertRaisesRegex(RuntimeError, "not signed"):
                    release_sign.verify_archive(archive, True, "expected-team")

    def test_universal_binary_checks_both_slices(self):
        def tools(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, "arm64 x86_64\n" if "-archs" in args else "", "")
        with patch.object(release_sign, "command", side_effect=tools), \
             patch.object(release_sign, "signature_slice", return_value="team") as signature:
            release_sign.signature(Path("binary"), True, "team")
        self.assertEqual([call.args[1] for call in signature.call_args_list], ["arm64", "x86_64"])

    def test_standalone_mcpb_must_match_embedded_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp) / "test.app"
            embedded = app / "Contents/Resources/agent-installers/d-bond-claude.mcpb"
            embedded.parent.mkdir(parents=True)
            embedded.write_bytes(b"embedded")
            standalone = Path(temp) / "different.mcpb"
            standalone.write_bytes(b"standalone")
            with patch.object(release_sign, "command"), patch.object(release_sign, "signature"), \
                 patch.object(release_sign, "macho_files", return_value=[]), \
                 patch.object(release_sign, "verify_archive", return_value=1):
                with self.assertRaisesRegex(RuntimeError, "Standalone MCPB differs"):
                    release_sign.verify(app, "developer-id", standalone)


if __name__ == "__main__":
    unittest.main()
