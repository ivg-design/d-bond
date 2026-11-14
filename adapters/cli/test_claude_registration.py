"""Native Claude attachment fixtures; never contact a host or broker."""
import json
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
