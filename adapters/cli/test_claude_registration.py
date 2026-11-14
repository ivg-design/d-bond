"""Native Claude attachment fixtures; never contact a host or broker."""
import json
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import claude_registration as registration
import dbond
import types


SESSION = "11111111-2222-3333-4444-555555555555"


class ClaudeRegistrationTests(unittest.TestCase):
    def test_cli_finishes_only_after_native_ready(self):
        args = types.SimpleNamespace(client="claude", session_id=SESSION, alias="agent")
        pairing = {"endpoint": "http://127.0.0.1:1234/v1/services/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/events"}
        for ready in (True, False):
            with patch.object(dbond, "bind_session"), patch.object(dbond, "_read_pairing", return_value=pairing), \
                 patch.object(dbond, "_enrollment_listener", return_value={"mode": "native_session", "ready": ready}), \
                 patch.object(dbond, "session_status", return_value={"wakeState": "offline"}) as status:
                result = dbond._enrollment_payload(args, pairing, reused=True)
                self.assertEqual(result["wake"]["state"], "offline")
                status.assert_called_once_with("agent", finish_registration=ready)

    def test_only_explicit_host_unsupported_uses_monitor_guidance(self):
        for state in ("held", "blocked", "offline", "unconfirmed", "waiting_host_bootstrap"):
            with patch.object(registration, "attach", return_value={"state": state, "mode": "native_session"}):
                result = dbond.claude_session_listener("agent", SESSION, {})
                self.assertEqual(result["mode"], "native_session")
                self.assertNotIn("command", result)
        with patch.object(registration, "attach", return_value={"state": "unsupported"}):
            self.assertEqual(dbond.claude_session_listener("agent", SESSION, {})["mode"], "foreground")

    def test_invalid_address_never_runs_helper(self):
        with patch.object(registration.subprocess, "run") as run:
            self.assertEqual(registration.attach("agent", "other")["state"], "blocked")
        run.assert_not_called()

    def test_missing_helper_is_blocked_not_an_unsupported_host(self):
        with patch.object(registration, "_helper", return_value=None):
            self.assertEqual(registration.attach("agent", SESSION)["state"], "blocked")

    def test_native_result_is_whitelisted_and_readiness_requires_host_support(self):
        result = {"state": "listening", "native": True, "ready": True, "inbox_supported": True,
                  "token": "never-expose", "config": "/private/config", "receiver_kind": "host_child"}
        with patch.object(registration, "_helper", return_value=Path("/fixture/listener.py")), \
             patch.object(registration.subprocess, "run") as run:
            run.return_value.stdout = json.dumps(result)
            attached = registration.attach("agent", SESSION)
            self.assertTrue(attached["ready"])
            self.assertNotIn("token", attached)
            self.assertNotIn("config", attached)
            self.assertEqual(run.call_args.args[0][3:], ["install", "--thread-id", SESSION, "--alias", "agent", "--json"])
            self.assertNotIn("env", run.call_args.kwargs)
            result.pop("inbox_supported")
            run.return_value.stdout = json.dumps(result)
            self.assertFalse(registration.attach("agent", SESSION)["ready"])

    def test_status_reads_health_once_without_installing_or_disclosing_private_fields(self):
        result = {"state": "listening", "ready": True, "native": True, "inbox_supported": True,
                  "socket_verified": True, "pid": 123, "token": "private", "worker_start": "private"}
        with patch.object(registration, "_helper", return_value=Path("/fixture/listener.py")), \
             patch.object(registration.subprocess, "run") as run:
            run.return_value = types.SimpleNamespace(stdout=json.dumps(result), returncode=0)
            value = registration.status(SESSION)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][3:], ["status", "--thread-id", SESSION, "--json"])
        self.assertTrue(value["ready"])
        self.assertEqual(value["state"], "listening")
        self.assertFalse({"pid", "token", "worker_start"} & value.keys())
        self.assertNotIn("wakeState", value)
        self.assertEqual(value["evidence"], "native_receiver_health_snapshot")

    def test_status_failure_and_missing_capability_are_not_ready(self):
        with patch.object(registration, "_helper", return_value=Path("/fixture/listener.py")), \
             patch.object(registration.subprocess, "run") as run:
            for result, code in (({"state": "listening", "ready": True, "native": True}, 0),
                                 ({"state": "listening", "ready": True, "native": True, "inbox_supported": True}, 1)):
                run.return_value = types.SimpleNamespace(stdout=json.dumps(result), returncode=code)
                self.assertFalse(registration.status(SESSION)["ready"])

    def test_session_status_reports_health_separately_from_subscribers_and_wake(self):
        pairing = {"client": "claude", "session_id": SESSION}
        owner = {"state": "subscriber_present", "wakeState": "pending"}
        receiver = {"state": "listening", "ready": True}
        with patch.object(dbond, "_read_pairing", return_value=pairing), \
             patch.object(dbond, "_session_status_readonly", return_value=owner.copy()), \
             patch.object(registration, "status", return_value=receiver) as status, \
             patch.object(dbond, "_owner_request") as request:
            # Internal status stays cheap; only the requested CLI display reads health.
            self.assertEqual(dbond.session_status("agent"), owner)
            status.assert_not_called()
            with contextlib.redirect_stdout(io.StringIO()) as output:
                dbond.command_session_status(types.SimpleNamespace(alias="agent", finish_registration=False))
            result = json.loads(output.getvalue())
        status.assert_called_once_with(SESSION)
        request.assert_not_called()
        self.assertEqual(result, {**owner, "receiver": receiver})

    def test_missing_health_helper_preserves_owner_status(self):
        owner = {"state": "unknown", "evidence": "owner_socket_unavailable"}
        with patch.object(dbond, "_read_pairing", return_value={"client": "claude", "session_id": SESSION}), \
             patch.object(dbond, "session_status", return_value=owner.copy()), \
             patch.object(registration, "_helper", return_value=None), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            dbond.command_session_status(types.SimpleNamespace(alias="agent", finish_registration=False))
        result = json.loads(output.getvalue())
        self.assertEqual(result["state"], owner["state"])
        self.assertEqual(result["evidence"], owner["evidence"])
        self.assertEqual(result["receiver"]["state"], "not_installed")
        self.assertFalse(result["receiver"]["ready"])

    def test_status_preserves_blocked_health_reason(self):
        with patch.object(registration, "_helper", return_value=Path("/fixture/listener.py")), \
             patch.object(registration.subprocess, "run") as run:
            run.return_value = types.SimpleNamespace(stdout=json.dumps({"state": "blocked", "ready": False,
                                                                       "reason": "fixture_block", "detail": "private"}), returncode=1)
            result = registration.status(SESSION)
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason"], "fixture_block")
        self.assertNotIn("detail", result)

    def test_uncertain_host_cannot_fall_back_as_unsupported(self):
        with patch.object(registration, "_helper", return_value=Path("/fixture/listener.py")), \
             patch.object(registration.subprocess, "run", side_effect=OSError()):
            self.assertEqual(registration.attach("agent", SESSION)["state"], "unconfirmed")

    def test_foreign_caller_has_bootstrap_guidance_without_claimed_receiver(self):
        with patch.object(registration, "_helper", return_value=Path("/fixture/listener.py")), \
             patch.object(registration.subprocess, "run") as run:
            run.return_value.stdout = json.dumps({"state": "waiting_host_bootstrap", "native": True,
                                                 "inbox_supported": True, "ready": False})
            value = registration.attach("agent", SESSION)
        self.assertFalse(value["ready"])
        self.assertIn("has not started", value["instruction"])
        self.assertIn("ordinary Bash command", value["instruction"])
        self.assertNotIn("owns incoming", value["instruction"])

    def test_verified_launch_opt_in_is_not_plugin_presence(self):
        with tempfile.TemporaryDirectory() as folder:
            helper = Path(folder) / "listener.py"
            helper.touch()
            inbox = helper.with_name("inbox.py")
            inbox.write_text("def discover_session(session_id):\n return {'native_channel': session_id == %r}\n" % SESSION)
            with patch.object(registration, "_helper", return_value=helper):
                self.assertTrue(registration.native_channel_opted_in(SESSION))
                self.assertFalse(registration.native_channel_opted_in("other"))
                inbox.write_text("raise RuntimeError('unsupported host')\n")
                self.assertFalse(registration.native_channel_opted_in(SESSION))


if __name__ == "__main__":
    unittest.main()
