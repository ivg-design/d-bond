"""Disposable host identity/wire fixtures. Never contacts a live Claude inbox."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import sys
import unittest
from unittest.mock import patch, MagicMock

spec = importlib.util.spec_from_file_location("inbox", Path(__file__).with_name("inbox.py"))
inbox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inbox)
SID = "44444444-4444-4444-8444-444444444444"
IS_DARWIN = sys.platform == "darwin"


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry = self.root / "registry"
        self.registry.mkdir(mode=0o700)
        self.sockdir = self.root / "sockets"
        self.sockdir.mkdir(mode=0o700)
        self.socket_path = self.sockdir / "42.sock"
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(self.socket_path))
        self.addCleanup(self.server.close)
        self.socket_path.chmod(0o600)
        self.exe = self.root / "claude"
        self.exe.write_text("fixture")
        self.exe.chmod(0o700)
        self.row = {"pid": 42, "sessionId": SID, "pidDomain": "darwin", "peerProtocol": 1,
                    "procStart": "Fri Nov 13 13:48:39 2026", "messagingSocketPath": str(self.socket_path)}
        self.record = self.registry / "42.json"
        self.write_record()
        self.proc = {"pid": 42, "ppid": 41, "uid": os.getuid(),
                     "proc_start": self.row["procStart"], "executable": str(self.exe)}
        for name, mock in (("sys.platform", "darwin"), ("_process", self.process),
                           ("_channel_admitted", lambda pid: False)):
            p = patch.object(inbox.sys, "platform", mock) if name == "sys.platform" else patch.object(inbox, name, mock)
            p.start()
            self.addCleanup(p.stop)

    def write_record(self):
        self.record.write_text(json.dumps(self.row))
        self.record.chmod(0o644)

    def process(self, pid):
        if pid == 42:
            return self.proc.copy()
        return dict(self.proc, pid=41, ppid=1, proc_start="parent start")

    def discover(self):
        return inbox.discover_session(SID, self.registry)

    def test_exact_live_identity_and_no_title_guessing(self):
        target = self.discover()
        self.assertEqual(target["pid"], 42)
        self.assertFalse(target["native_channel"])
        with self.assertRaises(inbox.InboxUnavailable):
            inbox.discover_session("f" * 8 + SID[8:], self.registry)

    def test_pid_reuse_domain_and_foreign_owner_fail_closed(self):
        self.proc["proc_start"] = "reused pid"
        with self.assertRaises(inbox.InboxUnavailable):
            self.discover()
        self.proc["proc_start"] = self.row["procStart"]
        self.proc["uid"] = os.getuid() + 1
        with self.assertRaises(inbox.InboxUnavailable):
            self.discover()
        self.proc["uid"] = os.getuid()
        self.row["pidDomain"] = "other-machine"
        self.write_record()
        with self.assertRaises(inbox.InboxUnavailable):
            self.discover()

    def test_socket_parent_permissions_and_symlink_are_rejected(self):
        self.sockdir.chmod(0o755)
        with self.assertRaises(inbox.InboxUnavailable):
            self.discover()
        self.sockdir.chmod(0o700)
        alternate = self.sockdir / "original.sock"
        self.socket_path.rename(alternate)
        self.socket_path.symlink_to(alternate)
        with self.assertRaises(inbox.InboxUnavailable):
            self.discover()

    def test_unsupported_contract_requires_live_identity_first(self):
        self.row["peerProtocol"] = 0
        self.write_record()
        self.proc["proc_start"] = "reused pid"
        with self.assertRaisesRegex(inbox.InboxUnavailable, "stale_or_foreign_process"):
            self.discover()
        self.proc["proc_start"] = self.row["procStart"]
        with self.assertRaisesRegex(inbox.InboxUnavailable, "unsupported_peer_domain_or_protocol"):
            self.discover()

    def test_verified_missing_inbox_is_unsupported_but_malformed_is_blocked(self):
        self.row.pop("messagingSocketPath")
        self.write_record()
        with self.assertRaisesRegex(inbox.InboxUnavailable, "unsupported_missing_inbox"):
            self.discover()
        self.row["messagingSocketPath"] = "relative.sock"
        self.write_record()
        with self.assertRaisesRegex(inbox.InboxUnavailable, "invalid_socket_path"):
            self.discover()
        self.row["messagingSocketPath"] = 42
        self.write_record()
        with self.assertRaisesRegex(inbox.InboxUnavailable, "invalid_socket_path"):
            self.discover()

    def fake_connection(self):
        context = MagicMock()
        context.__enter__.return_value = context
        return context

    def test_wire_stable_id_exact_uuid_and_peer_sender(self):
        target = self.discover()
        conn = self.fake_connection()
        with patch.object(inbox.socket, "socket", return_value=conn), patch.object(inbox, "_peer_credentials", return_value=(os.getuid(), 42)):
            result = inbox.deliver(target, "hello", "event-123", "Codex / design")
        frame = json.loads(conn.sendall.call_args.args[0])
        self.assertEqual(frame["session_id"], SID)
        self.assertEqual(frame["msg_id"], inbox.message_id(SID, "event-123"))
        self.assertEqual(frame["from"], "D-Bond")
        self.assertIn("Codex / design", frame["message"]["content"])
        self.assertNotIn("token", frame)
        self.assertNotIn("selfSent", frame)
        self.assertEqual(result["state"], "submitted_unknown")
        self.assertTrue(result["write_attempted"])

    def child_process(self, pid):
        if pid == os.getpid():
            return dict(self.proc, pid=pid, ppid=42, proc_start="child start")
        return self.process(pid)

    def test_inherited_child_capability_and_no_secret_in_results(self):
        target = self.discover()
        token = "ab" * 32
        with patch.dict(os.environ, {"CLAUDE_CODE_MESSAGING_SOCKET": str(self.socket_path),
                                    "CLAUDE_CODE_MESSAGING_TOKEN": token}), patch.object(inbox, "_process", self.child_process):
            context = inbox.validate_host_context(target)
        self.assertNotIn(token, repr(context))
        conn = self.fake_connection()
        with patch.object(inbox.socket, "socket", return_value=conn), patch.object(inbox, "_peer_credentials", return_value=(os.getuid(), 42)):
            result = inbox.deliver(target, "external content", "child-event", host_context=context)
        self.assertNotIn(token, json.dumps(result))
        auth, frame = map(json.loads, conn.sendall.call_args.args[0].splitlines())
        self.assertEqual(auth, {"type": "auth", "token": token})
        self.assertEqual(frame["from"], "D-Bond")
        self.assertNotIn("selfSent", frame)

    def test_copied_environment_without_own_ancestry_is_refused(self):
        target = self.discover()
        with patch.dict(os.environ, {"CLAUDE_CODE_MESSAGING_SOCKET": str(self.socket_path),
                                    "CLAUDE_CODE_MESSAGING_TOKEN": "ab" * 32}):
            with self.assertRaises(inbox.InboxUnavailable):
                inbox.validate_host_context(target)

    def test_inherited_capability_for_another_socket_is_refused(self):
        target = self.discover()
        with patch.dict(os.environ, {"CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/unrelated.sock",
                                    "CLAUDE_CODE_MESSAGING_TOKEN": "ab" * 32}), patch.object(inbox, "_process", self.child_process):
            with self.assertRaises(inbox.InboxUnavailable):
                inbox.validate_host_context(target)

    def test_validated_child_capability_cannot_target_a_restart(self):
        target = self.discover()
        context = inbox.HostContext(SID, 42, "old process start", target["socket_path"], "ab" * 32,
                                    inbox._host_identity(target))
        result = inbox.deliver(target, "hello", "child-event", host_context=context)
        self.assertEqual(result["reason"], "wrong_host_context")
        self.assertFalse(result["write_attempted"])

    def test_atomic_registry_rollover_keeps_same_host_and_child_capability(self):
        target = self.discover()
        with patch.dict(os.environ, {"CLAUDE_CODE_MESSAGING_SOCKET": str(self.socket_path),
                                    "CLAUDE_CODE_MESSAGING_TOKEN": "ab" * 32}), patch.object(inbox, "_process", self.child_process):
            context = inbox.validate_host_context(target)
        replacement = self.registry / "replacement.json"
        replacement.write_text(json.dumps(dict(self.row, status="idle")))
        replacement.chmod(0o644)
        replacement.replace(self.record)
        current = self.discover()
        self.assertNotEqual(target["record_identity"], current["record_identity"])
        self.assertTrue(inbox.same_host(target, current))
        with patch.dict(os.environ, {"CLAUDE_CODE_MESSAGING_SOCKET": str(self.socket_path),
                                    "CLAUDE_CODE_MESSAGING_TOKEN": "ab" * 32}), patch.object(inbox, "_process", self.child_process):
            inbox.validate_host_context(target)
        for destination in (target, current):
            conn = self.fake_connection()
            with patch.object(inbox.socket, "socket", return_value=conn), patch.object(inbox, "_peer_credentials", return_value=(os.getuid(), 42)):
                result = inbox.deliver(destination, "hello", "rollover-event", host_context=context)
            self.assertEqual(result["state"], "submitted_unknown")
            conn.sendall.assert_called_once()

    def test_child_context_refuses_replaced_socket_even_with_fresh_registry(self):
        target = self.discover()
        with patch.dict(os.environ, {"CLAUDE_CODE_MESSAGING_SOCKET": str(self.socket_path),
                                    "CLAUDE_CODE_MESSAGING_TOKEN": "ab" * 32}), patch.object(inbox, "_process", self.child_process):
            context = inbox.validate_host_context(target)
        self.socket_path.rename(self.sockdir / "retired.sock")
        other = socket.socket(socket.AF_UNIX)
        self.addCleanup(other.close)
        other.bind(str(self.socket_path))
        self.socket_path.chmod(0o600)
        current = self.discover()
        self.assertFalse(inbox.same_host(target, current))
        with patch.object(inbox.socket, "socket") as factory:
            result = inbox.deliver(current, "hello", "replacement-event", host_context=context)
        self.assertEqual(result["reason"], "wrong_host_context")
        factory.assert_not_called()

    def test_wrong_connected_pid_never_writes(self):
        target = self.discover()
        conn = self.fake_connection()
        with patch.object(inbox.socket, "socket", return_value=conn), patch.object(inbox, "_peer_credentials", return_value=(os.getuid(), 999)):
            result = inbox.deliver(target, "hello", "event-123")
        self.assertEqual(result["state"], "rejected")
        conn.sendall.assert_not_called()

    def test_partial_write_error_is_unknown_never_safe_to_retry(self):
        target = self.discover()
        conn = self.fake_connection()
        conn.sendall.side_effect = BrokenPipeError()
        with patch.object(inbox.socket, "socket", return_value=conn), patch.object(inbox, "_peer_credentials", return_value=(os.getuid(), 42)):
            result = inbox.deliver(target, "hello", "event-123")
        self.assertEqual(result["state"], "submitted_unknown")
        self.assertTrue(result["write_attempted"])

    def test_replaced_socket_and_oversized_wire_never_write(self):
        target = self.discover()
        self.socket_path.unlink()
        other = socket.socket(socket.AF_UNIX)
        self.addCleanup(other.close)
        other.bind(str(self.socket_path))
        self.socket_path.chmod(0o600)
        with patch.object(inbox.socket, "socket") as factory:
            result = inbox.deliver(target, "hello", "event-123")
            self.assertEqual(result["state"], "offline")
            factory.assert_not_called()
        result = inbox.deliver(target, "é" * inbox.MAX_FRAME_BYTES, "event-123")
        self.assertEqual(result["reason"], "message_too_large")

    @unittest.skipUnless(IS_DARWIN, "Darwin peer credential API")
    def test_real_os_credentials_on_disposable_socketpair(self):
        left, right = socket.socketpair()
        with left, right:
            self.assertEqual(inbox._peer_credentials(left), (os.getuid(), os.getpid()))


if __name__ == "__main__":
    unittest.main()
