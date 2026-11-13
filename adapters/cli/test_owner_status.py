"""Read-only local app discovery; no real app or pairing is contacted."""
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import owner


class LocalStatusTests(unittest.TestCase):
    def test_reachable_does_not_expose_owner_response(self):
        with mock.patch.object(owner, "request", return_value={"services": [{"secret": "never-return"}]}) as request:
            self.assertEqual(owner.local_status(), {"installation": "installed", "service": "reachable"})
        request.assert_called_once_with({"op": "list"}, timeout=0.75)

    def test_installed_app_with_missing_service_is_offline(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            app = home / "Applications/D-Bond.app/Contents"
            (app / "MacOS").mkdir(parents=True)
            (app / "MacOS/DBond").touch()
            (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleName": "D-Bond", "CFBundleExecutable": "DBond"}))
            with mock.patch.object(owner.Path, "home", return_value=home), \
                 mock.patch.object(owner, "request", side_effect=FileNotFoundError), \
                 mock.patch.object(owner.subprocess, "run") as registry:
                self.assertEqual(owner.local_status(), {"installation": "installed", "service": "offline"})
                registry.assert_not_called()

    def test_registry_absent_and_unavailable_are_distinct(self):
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(owner.Path, "home", return_value=Path(folder)), \
                 mock.patch.object(owner, "request", side_effect=FileNotFoundError), \
                 mock.patch.object(owner.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
                self.assertEqual(owner.local_status(), {"installation": "uninstalled", "service": "offline"})
            with mock.patch.object(owner.Path, "home", return_value=Path(folder)), \
                 mock.patch.object(owner, "request", side_effect=FileNotFoundError), \
                 mock.patch.object(owner.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 0.75)):
                self.assertEqual(owner.local_status(), {"installation": "unknown", "service": "unknown"})


if __name__ == "__main__":
    unittest.main()
