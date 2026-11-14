"""Exact native enrollment/channel admission; no live transport or installation."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "adapters/cli"))
spec = importlib.util.spec_from_file_location("claude_native_mcp", ROOT / "adapters/claude/server/mcp.py")
mcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp)
SESSION = "11111111-2222-3333-4444-555555555555"


class NativeEnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(mcp, "CHANNEL", False),
                        patch.object(mcp, "CHANNEL_ADMITTED", False),
                        patch.object(mcp, "CHANNEL_WORKERS_STARTED", False),
                        patch.object(mcp.dbond, "bind_session", return_value={"state": "bound"}),
                        patch.object(mcp.dbond, "_read_pairing", return_value={}),
                        patch.object(mcp.dbond, "session_status", return_value={"state": "subscriber_present"})]
        for p in self.patches:
            p.start()
        self.addCleanup(patch.stopall)
        mcp.CHANNEL_ACTIVE.clear()
        mcp.CHANNEL_INITIALIZED.clear()

    def test_native_receiver_ready_finishes_registration_without_monitor(self):
        with patch.object(mcp.dbond, "claude_session_listener", return_value={"mode": "native_session", "ready": True}):
            value = mcp.enrollment_result({}, "agent", SESSION)
        self.assertEqual(value["activation"]["state"], "native_session_ready")
        self.assertNotIn("command", value["listener"])
        mcp.dbond.session_status.assert_called_once_with("agent", finish_registration=True)

    def test_unconfirmed_or_held_receiver_never_falls_back(self):
        for state in ("held", "unconfirmed", "offline", "waiting_existing_listener"):
            with patch.object(mcp.dbond, "claude_session_listener", return_value={"state": state, "mode": "native_session", "ready": False}):
                value = mcp.enrollment_result({}, "agent", SESSION)
            self.assertEqual(value["activation"]["state"], "native_session_starting")
            self.assertNotIn("command", value["listener"])

    def test_admitted_initialized_channel_has_priority(self):
        mcp.CHANNEL = mcp.CHANNEL_ADMITTED = True
        mcp.CHANNEL_ACTIVE.set()
        with patch.object(mcp, "start_receipt_retry"), patch.object(mcp.dbond, "claude_session_listener") as native:
            value = mcp.enrollment_result({}, "agent", SESSION)
        self.assertEqual(value["listener"]["mode"], "native_channel")
        native.assert_not_called()

    def test_plugin_presence_without_host_admission_uses_native_receiver(self):
        mcp.CHANNEL = True
        with patch.object(mcp, "start_receipt_retry"), \
             patch.object(mcp.dbond, "claude_session_listener", return_value={"mode": "native_session", "ready": False}) as native:
            value = mcp.enrollment_result({}, "agent", SESSION)
        self.assertEqual(value["listener"]["mode"], "native_session")
        native.assert_called_once()

    def test_legacy_client_requires_verified_exact_launch(self):
        with patch.object(mcp.dbond, "_initiating_claude", return_value=(SESSION, "Title")), \
             patch.dict(sys.modules, {"claude_registration": types.SimpleNamespace(native_channel_opted_in=lambda sid: sid == SESSION)}):
            self.assertTrue(mcp.client_admits_channel({"capabilities": {}}))
        with patch.object(mcp.dbond, "_initiating_claude", return_value=None):
            self.assertFalse(mcp.client_admits_channel({"capabilities": {}}))

    def test_late_channel_admission_after_handshake_starts_workers_once(self):
        mcp.CHANNEL = True
        with patch.object(mcp, "client_admits_channel", side_effect=[False, True]) as admit, \
             patch.object(mcp, "initialize_channel_identity", return_value="agent"), \
             patch.object(mcp, "start_receipt_retry"), patch.object(mcp.threading, "Thread") as thread:
            self.assertFalse(mcp.ensure_channel_listener())
            admit.assert_not_called()  # An uninitialized client cannot admit delivery.
            mcp.CHANNEL_INITIALIZED.set()
            self.assertFalse(mcp.ensure_channel_listener())
            thread.assert_not_called()
            self.assertTrue(mcp.ensure_channel_listener())
            self.assertTrue(mcp.ensure_channel_listener())
            self.assertEqual(thread.call_count, 2)
            self.assertTrue(mcp.channel_admitted())

    def test_enrollment_rechecks_late_channel_before_native_helper(self):
        mcp.CHANNEL = True
        mcp.CHANNEL_INITIALIZED.set()
        with patch.object(mcp, "client_admits_channel", return_value=True), \
             patch.object(mcp, "initialize_channel_identity", return_value="agent"), \
             patch.object(mcp, "start_receipt_retry"), patch.object(mcp.threading, "Thread"), \
             patch.object(mcp.dbond, "claude_session_listener") as native:
            value = mcp.enrollment_result({}, "agent", SESSION)
        native.assert_not_called()
        self.assertEqual(value["listener"]["mode"], "native_channel")


if __name__ == "__main__":
    unittest.main()
