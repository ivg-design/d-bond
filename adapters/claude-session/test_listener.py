#!/usr/bin/env python3
"""Isolated receiver fixtures: never connect to Claude or the production broker."""
import copy
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import listener as app

THREAD = "11111111-1111-4111-8111-111111111111"
SERVICE = "22222222-2222-4222-8222-222222222222"
SENDER = "33333333-3333-4333-8333-333333333333"


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = patch.object(app, "STATE_DIR", Path(self.tmp.name) / "state")
        self.state.start()
        self.bound = {"thread_id": THREAD, "alias": "claude-test", "service_id": SERVICE}
        self.pair = {"endpoint": "http://127.0.0.1:1234/services/" + SERVICE,
                     "session_id": THREAD, "client": "claude", "destination": "dot",
                     "direct_cursor": 0, "cursor": 0}
        self.binding = patch.object(app, "binding", side_effect=lambda _: (self.bound, copy.deepcopy(self.pair)))
        self.binding.start()
        def save(alias, pair):
            self.pair = copy.deepcopy(pair)
        self.save = patch.object(app.dbond, "_save", side_effect=save).start()
        self.send = patch.object(app.inbox, "deliver", return_value={"state": "submitted_unknown"}).start()
        self.receipt = patch.object(app.dbond, "direct_delivery_receipt", return_value={"state": "receipt_queued"}).start()
        self.receiver = app.Receiver(THREAD, self.bound, self.pair, Mock())
        self.event = {"id": "msg-test-message", "from": SENDER, "to": SERVICE,
                      "sequence": 1, "text": "Original external payload"}
        self.row = {"state": "direct_message", "source_id": self.event["id"],
                    "sender_id": SENDER, "sequence": 1, "text": self.event["text"], "in_reply_to": None}
        self.original_normalize = app.dbond._session_row
        self.normalize = patch.object(app.dbond, "_session_row", return_value=self.row).start()

    def tearDown(self):
        patch.stopall()
        self.tmp.cleanup()

    def test_unknown_write_is_durable_and_not_host_ack(self):
        self.assertEqual(self.receiver.process(self.event, "direct", {}), "submitted_unknown")
        self.assertEqual(self.pair["direct_cursor"], 1)
        self.assertEqual(self.receipt.call_args.kwargs["state"], "unconfirmed")
        self.assertEqual(self.receipt.call_args.kwargs["basis"], "host_result_unknown")
        self.assertNotIn("participant", str(self.receipt.call_args))

    def test_duplicate_sequence_never_dispatches(self):
        self.pair["direct_cursor"] = 1
        self.receiver.process(self.event, "direct", {})
        self.send.assert_not_called()

    def test_consumed_history_advances_without_dispatch(self):
        self.event["recipient_consumed"] = True
        self.receiver.process(self.event, "direct", {})
        self.send.assert_not_called()
        self.receipt.assert_not_called()
        self.assertEqual(self.pair["direct_cursor"], 1)

    def test_prepared_crash_never_repeats_possible_write(self):
        path = app.paths(THREAD)["journal"] / "direct-1.json"
        app.save_json(path, {"source_id": self.row["source_id"], "state": "prepared_unknown"})
        self.receiver.process(self.event, "direct", {})
        self.send.assert_not_called()
        self.assertEqual(self.pair["direct_cursor"], 1)

    def test_completed_journal_with_unadvanced_cursor_never_repeats(self):
        self.receiver.process(self.event, "direct", {})
        self.pair["direct_cursor"] = 0
        self.receiver.process(self.event, "direct", {})
        self.assertEqual(self.send.call_count, 1)

    def test_prewrite_offline_keeps_cursor_and_can_retry(self):
        self.send.return_value = {"state": "offline", "write_attempted": False}
        with self.assertRaises(ConnectionError):
            self.receiver.process(self.event, "direct", {})
        self.assertEqual(self.pair["direct_cursor"], 0)
        self.assertFalse((app.paths(THREAD)["journal"] / "direct-1.json").exists())
        self.receipt.assert_not_called()

    def test_dispatch_exception_quarantines(self):
        self.send.side_effect = OSError("possible partial write")
        self.receiver.process(self.event, "direct", {})
        self.assertEqual(self.pair["direct_cursor"], 1)
        self.assertEqual(app.read_json(app.paths(THREAD)["journal"] / "direct-1.json")["state"], "submitted_unknown")

    def test_journal_mismatch_stops_without_advancing(self):
        app.save_json(app.paths(THREAD)["journal"] / "direct-1.json", {"source_id": "other", "state": "prepared_unknown"})
        with self.assertRaises(ValueError):
            self.receiver.process(self.event, "direct", {})
        self.send.assert_not_called()
        self.assertEqual(self.pair["direct_cursor"], 0)

    def test_payload_has_provenance_and_external_text(self):
        text = app.prompt(self.row, "claude-test")
        self.assertIn("Message from the paired sender:\n" + self.event["text"], text)
        for value in ("Alias: claude-test", "Source ID: " + self.event["id"], "Sequence: 1", "Sender ID: " + SENDER):
            self.assertIn(value, text)
        self.assertIn("silently call acknowledge_direct_message", text)
        self.assertIn("Pairing authorizes ordinary two-way communication, replies, and requested diagnostics", text)
        self.assertIn(f"~/.local/bin/dbond direct-ack claude-test {SENDER} msg-test-message 1", text)
        self.assertNotIn("--id", text)

    def test_wake_check_uses_wake_ack_and_readable_multiline_body(self):
        row = dict(self.row, source_id="@wake:" + THREAD, sender_id="owner", text="First line\nSecond line")
        text = app.prompt(row, "claude-test")
        self.assertIn("~/.local/bin/dbond wake-ack claude-test 1", text)
        self.assertNotIn("direct-ack", text)
        self.assertIn("First line\nSecond line", text)
        self.assertNotIn("First line\\nSecond line", text)

    def test_dot_lane_preserves_push_history_and_separate_cursor(self):
        self.row.update(state="dot_push", source_id="@dot:8")
        self.row.pop("sender_id")
        self.receiver.process(self.event, "dot", {})
        self.assertEqual(self.pair["cursor"], 1)
        self.assertEqual(self.pair["direct_cursor"], 0)
        self.assertIn("@dot:8", self.pair["pushes"])
        self.receipt.assert_not_called()

    def test_internal_nonmessage_advances_without_dispatch(self):
        self.normalize.return_value = None
        self.receiver.process(self.event, "direct", {})
        self.send.assert_not_called()
        self.assertEqual(self.pair["direct_cursor"], 1)

    def test_invalid_sequence_fails_without_dispatch(self):
        self.event["sequence"] = True
        with self.assertRaises(ValueError):
            self.receiver.process(self.event, "direct", {})
        self.send.assert_not_called()

    def test_state_rejects_symlink(self):
        path = Path(self.tmp.name) / "link"
        target = Path(self.tmp.name) / "target"
        app.save_json(target, {"key": "value"})
        path.symlink_to(target)
        with self.assertRaises(OSError):
            app.read_json(path)

    def test_install_never_launches_when_native_channel_owned(self):
        with patch.object(app.dbond, "session_binding", return_value=self.bound), \
             patch.object(app.inbox, "discover_session", return_value={"native_channel": True}), \
             patch.object(app.subprocess, "run") as proc:
            result = app.install(THREAD, "claude-test")
        self.assertEqual(result["state"], "native_channel_owned")
        proc.assert_not_called()

    def test_foreign_bootstrap_never_spawns_or_writes_binding(self):
        with patch.object(app.dbond, "session_binding", return_value=self.bound), \
             patch.object(app.inbox, "discover_session", return_value={"pid": 8}), \
             patch.object(app.inbox, "validate_host_context", side_effect=app.inbox.InboxUnavailable("not_child")), \
             patch.object(app.subprocess, "Popen") as child:
            result = app.install(THREAD, "claude-test")
        self.assertEqual(result["state"], "waiting_host_bootstrap")
        self.assertFalse(app.paths(THREAD)["binding"].exists())
        child.assert_not_called()

    def test_real_shared_normalizer_preserves_original_direct_payload(self):
        self.normalize.side_effect = self.original_normalize
        self.receiver.process(self.event, "direct", {"pid": 1})
        self.assertIn(self.event["text"], self.send.call_args.args[1])
        self.assertIn(self.event["id"], self.send.call_args.args[1])

    def test_real_shared_normalizer_handles_addressed_dot_push(self):
        self.normalize.side_effect = self.original_normalize
        event = {"state": "dot_push", "id": "@dot:7", "sequence": 7, "reply": "Dot original body"}
        self.receiver.process(event, "dot", {})
        self.assertEqual(self.pair["cursor"], 7)
        self.assertIn("Dot original body", self.send.call_args.args[1])

    def test_record_rewrite_is_not_host_replacement(self):
        host = {"session_id": THREAD, "pid": 11, "proc_start": "date", "socket_path": "/tmp/x",
                "socket_identity": [1, 2], "executable_identity": [1, 3], "record_identity": [1, 4]}
        changed = dict(host, record_identity=[1, 5])
        self.assertTrue(app.same_host(host, changed))
        self.assertFalse(app.same_host(host, dict(host, proc_start="new")))
        self.assertFalse(app.same_host(host, dict(host, socket_identity=[1, 8])))

    def test_old_monitor_is_not_duplicated_and_host_exit_stops_receiver(self):
        target = {"pid": 10, "session_id": THREAD, "proc_start": "start", "native_channel": False}
        wake = Mock()
        with patch.object(app.inbox, "discover_session", side_effect=[target, target, app.inbox.InboxUnavailable("ended")]), \
             patch.object(app.inbox, "validate_host_context", return_value=object()), \
             patch.object(app, "HostWake", return_value=wake), \
             patch.object(app, "health") as health, \
             patch.object(app.signal, "signal"), \
             patch.object(app.dbond, "session_status", return_value={"state": "subscriber_present"}), \
             patch.object(app.dbond, "Stream") as stream:
            app.private_dir(app.paths(THREAD)["dir"])
            result = app.run(THREAD)
        self.assertEqual(result["state"], "host_ended")
        stream.assert_not_called()
        self.assertTrue(any(c.kwargs.get("state") == "waiting_existing_listener" for c in health.call_args_list))
        wake.close.assert_called_once()

    def test_registry_record_churn_does_not_restart_healthy_streams(self):
        target = {"pid": 10, "session_id": THREAD, "proc_start": "start", "native_channel": False,
                  "record_identity": [1, 1]}
        rewritten = dict(target, record_identity=[1, 2])
        receiver = Mock()
        receiver.failure = None
        receiver.connected = {"direct", "dot"}
        receiver.lock = threading.Lock()
        receiver.last_outcome = None
        with patch.object(app.inbox, "discover_session", side_effect=[target, target, rewritten, app.inbox.InboxUnavailable("ended")]), \
             patch.object(app.inbox, "validate_host_context", return_value=object()), \
             patch.object(app, "HostWake", return_value=Mock()), \
             patch.object(app, "health"), patch.object(app.signal, "signal"), \
             patch.object(app.dbond, "session_status", return_value={"state": "pending_listener"}) as observed, \
             patch.object(app, "Receiver", return_value=receiver) as factory, \
             patch.object(app.threading, "Thread"):
            app.run(THREAD)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(observed.call_count, 1)

    def test_main_only_downgrades_verified_unsupported_capabilities(self):
        for reason in ("macos_only", "unsupported_peer_domain_or_protocol", "unsupported_missing_inbox",
                       "unsafe_socket", "record_replaced", "stale_or_foreign_process", "invalid_socket_path",
                       "no_unique_live_session"):
            with self.subTest(reason=reason), \
                 patch.object(app.sys, "argv", ["listener.py", "install", "--thread-id", THREAD, "--alias", "claude-test"]), \
                 patch.object(app, "install", side_effect=app.inbox.InboxUnavailable(reason)):
                output = io.StringIO()
                with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
                    app.main()
                result = json.loads(output.getvalue())
                unsupported = reason in {"macos_only", "unsupported_peer_domain_or_protocol", "unsupported_missing_inbox"}
                self.assertEqual(result["state"], "unsupported" if unsupported else "blocked")
                self.assertEqual(result["reason"], reason)
                self.assertFalse(result["ready"])
                if unsupported:
                    self.assertFalse(result["native"])
                    self.assertFalse(result["inbox_supported"])

    @unittest.skipUnless(hasattr(app.select, "kqueue"), "macOS kernel watcher")
    def test_kernel_registry_change_wakes_without_periodic_poll(self):
        registry = Path(self.tmp.name) / "registry"
        registry.mkdir(mode=0o700)
        wake = app.HostWake(registry)
        result = []
        worker = threading.Thread(target=lambda: result.append(wake.wait()), daemon=True)
        worker.start()
        try:
            (registry / "fixture.json").write_text("{}")
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertTrue(result[0])
        finally:
            if worker.is_alive():
                wake.notify()
                worker.join(2)
            wake.close()


if __name__ == "__main__":
    unittest.main()
