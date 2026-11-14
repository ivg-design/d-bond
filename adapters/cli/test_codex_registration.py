"""Read-only Codex health fixtures; never contact a host or broker."""
import contextlib
import io
import json
import subprocess
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_registration as registration
import dbond

THREAD = "11111111-2222-3333-4444-555555555555"
SERVICE = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
STAMP = "2026-11-14T09:12:35-05:00"
HEALTH = {"thread_id": THREAD, "service_id": SERVICE, "state": "degraded", "ready": False,
          "host": "owner_offline", "host_id": "durable", "transport": "subscribed",
          "pending": {"direct": {"sequence": 42, "firstObservedAt": STAMP, "ownerState": "unavailable",
              "ownerUnavailableFirstAt": STAMP, "ownerUnavailableLastAt": STAMP, "reason": "owner_offline"}}}


class CodexHealthTests(unittest.TestCase):
    def read_status(self, value=HEALTH, returncode=0):
        with patch.object(registration, "_helper", return_value=Path("/fixture/listener.py")), \
             patch.object(registration.subprocess, "run") as run:
            run.return_value = types.SimpleNamespace(stdout=json.dumps(value), returncode=returncode)
            result = registration.status(THREAD, SERVICE)
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0][3:], ["status", "--thread-id", THREAD, "--json"])
            self.assertEqual(run.call_args.kwargs["timeout"], 8)
            self.assertNotIn("env", run.call_args.kwargs)
            return result

    def test_pending_times_and_host_are_public_but_private_fields_and_wake_are_not(self):
        raw = {**HEALTH, "ready": True, "token": "secret", "config": "/private/config",
               "wakeState": "verified", "lastVerifiedAt": "historical",
               "pending": {"direct": {**HEALTH["pending"]["direct"], "text": "private", "ownerToken": "secret"}}}
        result = self.read_status(raw)
        self.assertEqual(result["pending"], HEALTH["pending"])
        self.assertEqual(result["host_id"], "durable")
        self.assertFalse(result["ready"])
        self.assertEqual(result["evidence"], "codex_listener_health_snapshot")
        for forbidden in ("secret", "private", "historical", "wakeState"):
            self.assertNotIn(forbidden, json.dumps(result))

    def test_wrong_thread_or_service_never_exposes_foreign_snapshot(self):
        for key, foreign in (("thread_id", SERVICE), ("service_id", THREAD), ("service_id", None)):
            result = self.read_status({**HEALTH, key: foreign})
            self.assertEqual(result, {"state": "unconfirmed", "ready": False,
                                      "reason": "codex_listener_health_unavailable"})

    def test_failed_or_malformed_health_is_unconfirmed(self):
        for value, code in ((HEALTH, 1), ([], 0), ({**HEALTH, "pending": {"direct": {"sequence": True}}}, 0)):
            self.assertEqual(self.read_status(value, code)["state"], "unconfirmed")
        with patch.object(registration, "_helper", return_value=Path("/fixture/listener.py")), \
             patch.object(registration.subprocess, "run", side_effect=subprocess.TimeoutExpired("fixture", 8)):
            self.assertFalse(registration.status(THREAD, SERVICE)["ready"])

    def test_invalid_identity_and_missing_helper_do_not_run_any_command(self):
        with patch.object(registration, "_helper", return_value=None), \
             patch.object(registration.subprocess, "run") as run:
            self.assertEqual(registration.status("other", SERVICE)["state"], "blocked")
            self.assertEqual(registration.status(THREAD, SERVICE)["state"], "not_installed")
        run.assert_not_called()

    def test_cli_adds_health_without_changing_internal_or_historical_wake_status(self):
        pairing = {"client": "codex", "session_id": THREAD,
                   "endpoint": f"http://127.0.0.1:1234/v1/services/{SERVICE}/events"}
        owner = {"state": "subscriber_present", "wakeState": "verified", "lastWakeVerifiedAt": "historical"}
        with patch.object(dbond, "_read_pairing", return_value=pairing), \
             patch.object(dbond, "_session_status_readonly", side_effect=lambda _: owner.copy()), \
             patch.object(registration, "status", return_value=HEALTH) as status, \
             patch.object(registration, "attach") as attach, patch.object(dbond, "_owner_request") as request:
            self.assertEqual(dbond.session_status("agent"), owner)
            status.assert_not_called()
            with contextlib.redirect_stdout(io.StringIO()) as output:
                dbond.command_session_status(types.SimpleNamespace(alias="agent", finish_registration=False))
            self.assertEqual(json.loads(output.getvalue()), {**owner, "receiver": HEALTH})
        status.assert_called_once_with(THREAD, SERVICE)
        attach.assert_not_called()
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
