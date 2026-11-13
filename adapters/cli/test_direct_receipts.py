"""Delivery metadata tests: no installed app, live pairings, or peer messages."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import dbond


OWN = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PEER = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
PAIR = {"endpoint": f"http://127.0.0.1:1234/v1/services/{OWN}/events", "secret": "fixture"}
EVENT = {"from": PEER, "to": OWN, "id": "message-1", "sequence": 7, "text": "Private content"}


class DirectReceiptTests(unittest.TestCase):
    def send(self, responses, **kwargs):
        with patch.object(dbond, "_direct_target", return_value=PEER), \
                patch.object(dbond, "_request", side_effect=responses) as request:
            result = dbond.direct_send_message(PAIR, PEER, "Hello", message_id="fixed-id", **kwargs)
        return result, request.call_args_list

    def test_send_waits_once_and_returns_concise_authenticated_ack(self):
        result, calls = self.send([(202, {"id": "fixed-id", "sequence": 8, "duplicate": False}),
                                   (200, {"id": "fixed-id", "sequence": 8, "state": "acknowledged",
                                          "satisfied": True, "basis": "host_acceptance", "adapter": "codex-session"})])
        self.assertEqual(result, {"id": "fixed-id", "to": PEER, "sequence": 8, "duplicate": False,
                                  "state": "acknowledged", "basis": "host_acceptance", "adapter": "codex-session"})
        self.assertEqual(len(calls), 2)
        self.assertIn("condition=acknowledged&timeout_ms=10000", calls[1].args[2])
        self.assertEqual(calls[1].kwargs["timeout"], 12)

    def test_offline_timeout_keeps_durable_send_queued_without_resending(self):
        stored = (200, {"id": "fixed-id", "sequence": 8, "duplicate": True})
        pending = (200, {"id": "fixed-id", "sequence": 8, "state": "pending", "satisfied": False})
        result, calls = self.send([stored, pending], wait_ms=0)
        self.assertEqual(result["state"], "queued")
        self.assertTrue(result["duplicate"])
        self.assertEqual([call.args[1] for call in calls], ["POST", "GET"])
        result, _ = self.send([stored, SystemExit(1)], in_reply_to="original")
        self.assertEqual(result["state"], "queued")
        self.assertEqual(result["delivery_status"], "unconfirmed")

    def test_received_is_not_acknowledged(self):
        result, _ = self.send([(202, {"id": "fixed-id", "sequence": 8}),
                               (200, {"id": "fixed-id", "sequence": 8, "state": "received",
                                      "satisfied": False, "basis": "host_acceptance"})])
        self.assertEqual(result["state"], "received")

    def test_receipt_outbox_retries_metadata_without_repeating_message(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(dbond, "_safe_dir", return_value=Path(folder)), \
                patch.object(dbond, "_request", side_effect=SystemExit(1)):
            result = dbond.direct_delivery_receipt(PAIR, EVENT, adapter="claude-channel")
            self.assertEqual(result["state"], "receipt_queued")
            paths = list(Path(folder).glob(".receipt-*.json"))
            self.assertEqual(len(paths), 1)
            self.assertEqual(paths[0].stat().st_mode & 0o777, 0o600)
            saved = json.loads(paths[0].read_text())
            self.assertNotIn("secret", saved)
            self.assertNotIn("text", saved["body"])
            # Same receipt retry overwrites one stable outbox item.
            dbond.direct_delivery_receipt(PAIR, EVENT, adapter="claude-channel")
            self.assertEqual(len(list(Path(folder).glob(".receipt-*.json"))), 1)
            with patch.object(dbond, "_request", return_value=(200, {"receipt": saved["body"], "duplicate": True})) as request:
                dbond.retry_direct_receipts(PAIR)
            self.assertEqual(request.call_count, 1)
            self.assertTrue(request.call_args.args[2].endswith("/messages/receipt"))
            self.assertEqual(list(Path(folder).glob(".receipt-*.json")), [])

    def test_participant_ack_uses_exact_sender_source_and_sequence(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(dbond, "_safe_dir", return_value=Path(folder)), \
                patch.object(dbond, "_request", side_effect=lambda _pair, _method, _url, body, **kwargs:
                             (200, {"receipt": body, "duplicate": False})) as request:
            dbond.direct_delivery_receipt(PAIR, {"sender_id": PEER, "source_id": "message-1", "sequence": 7},
                                          adapter="claude-agent", state="acknowledged", basis="participant")
            self.assertEqual(request.call_args.args[3], {"from": PEER, "id": "message-1", "sequence": 7,
                             "state": "acknowledged", "basis": "participant", "adapter": "claude-agent"})
            with self.assertRaises(ValueError):
                dbond.direct_delivery_receipt(PAIR, EVENT, adapter="invalid adapter")
            with self.assertRaises(ValueError):
                dbond.direct_delivery_receipt(PAIR, EVENT, adapter="cli", basis="participant")
            with self.assertRaises(ValueError):
                dbond.direct_delivery_receipt(PAIR, {**EVENT, "to": PEER}, adapter="cli")

    def test_uncertain_post_preserves_the_generated_retry_id(self):
        with patch.object(dbond, "_direct_target", return_value=PEER), \
                patch.object(dbond.uuid, "uuid4", return_value="new-stable-id"), \
                patch.object(dbond, "_request", side_effect=SystemExit(1)):
            with self.assertRaisesRegex(ValueError, "new-stable-id.*same ID"):
                dbond.direct_send_message(PAIR, PEER, "Hello")

    def test_retry_worker_scans_once_then_blocks_idle_and_stops(self):
        stop, scanned = threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(dbond, "_safe_dir", return_value=Path(folder)), \
                patch.object(dbond, "retry_direct_receipts", side_effect=lambda _: (scanned.set(), 0)[1]) as scan:
            worker = threading.Thread(target=dbond.receipt_retry_loop, args=(PAIR, stop))
            worker.start()
            try:
                self.assertTrue(scanned.wait(2))
                # More than the former polling interval: no repeated idle scan.
                time.sleep(2.2)
                self.assertEqual(scan.call_count, 1)
            finally:
                stop.set()
                worker.join(2)
            self.assertFalse(worker.is_alive())

    def test_in_process_queue_signal_wakes_an_idle_worker(self):
        stop, initial, retried = threading.Event(), threading.Event(), threading.Event()
        def scan(_):
            if initial.is_set(): retried.set()
            initial.set()
            return 0
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(dbond, "_safe_dir", return_value=Path(folder)), \
                patch.object(dbond, "retry_direct_receipts", side_effect=scan):
            worker = threading.Thread(target=dbond.receipt_retry_loop, args=(PAIR, stop))
            worker.start()
            try:
                self.assertTrue(initial.wait(2))
                dbond._signal_receipt_retry(PAIR)
                self.assertTrue(retried.wait(2))
            finally:
                stop.set(); worker.join(2)
            self.assertFalse(worker.is_alive())

    def test_retry_backoff_exists_only_while_this_binding_has_pending_receipts(self):
        stop = threading.Event()
        waits = []
        class Wake:
            def __init__(self, *_): pass
            def wait(self, delay):
                waits.append(delay)
                if delay is None: stop.set()
            def close(self): pass
        with patch.object(dbond, "_ReceiptRetryWake", Wake), \
                patch.object(dbond, "retry_direct_receipts", side_effect=[1] * 7 + [0]):
            dbond.receipt_retry_loop(PAIR, stop)
        self.assertEqual(waits, [1., 2., 4., 8., 16., 30., 30., None])

    def test_watcher_does_not_repeat_first_receipt_post_while_in_flight(self):
        entered, finish = threading.Event(), threading.Event()
        def request(_p, _m, _u, body, **kwargs):
            entered.set()
            finish.wait(2)
            return 200, {"receipt": body}
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(dbond, "_safe_dir", return_value=Path(folder)), \
                patch.object(dbond, "_request", side_effect=request) as post:
            worker = threading.Thread(target=dbond.direct_delivery_receipt, args=(PAIR, EVENT), kwargs={"adapter": "claude-channel"})
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(dbond.retry_direct_receipts(PAIR), 0)
                self.assertEqual(post.call_count, 1)
            finally:
                finish.set(); worker.join(2)
            self.assertFalse(worker.is_alive())

    @unittest.skipUnless(sys.platform == "darwin", "macOS kqueue cross-process outbox watcher")
    def test_macos_external_queue_wakes_idle_binding_and_replays(self):
        stop, initial, delivered = threading.Event(), threading.Event(), threading.Event()
        original = dbond.retry_direct_receipts
        def scan(pair):
            value = original(pair)
            initial.set()
            return value
        def request(_pair, _method, _url, body, **kwargs):
            delivered.set()
            return 200, {"receipt": body, "duplicate": False}
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(dbond, "_safe_dir", return_value=Path(folder)), \
                patch.object(dbond, "retry_direct_receipts", side_effect=scan), \
                patch.object(dbond, "_request", side_effect=request):
            worker = threading.Thread(target=dbond.receipt_retry_loop, args=(PAIR, stop))
            worker.start()
            try:
                self.assertTrue(initial.wait(2))
                body = {"from": PEER, "id": "message-1", "sequence": 7, "state": "acknowledged",
                        "adapter": "external-agent", "basis": "participant"}
                value = json.dumps({"endpoint": PAIR["endpoint"], "body": body})
                subprocess.run([sys.executable, "-c", "import os,sys; p=sys.argv[1]; f=os.open(p,os.O_WRONLY|os.O_CREAT,0o600); os.write(f,sys.argv[2].encode()); os.close(f)",
                                str(Path(folder) / ".receipt-external.json"), value], check=True)
                self.assertTrue(delivered.wait(2), "external outbox write did not wake kqueue")
                until = time.monotonic() + 2
                while list(Path(folder).glob(".receipt-*.json")) and time.monotonic() < until:
                    time.sleep(.01)
                self.assertEqual(list(Path(folder).glob(".receipt-*.json")), [])
            finally:
                stop.set(); worker.join(2)
            self.assertFalse(worker.is_alive())

    def test_unknown_host_outcome_receipt_and_status_stay_unconfirmed(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(dbond, "_safe_dir", return_value=Path(folder)), \
                patch.object(dbond, "_request", side_effect=lambda _p, _m, _u, body, **kw: (200, {"receipt": body})):
            result = dbond.direct_delivery_receipt(PAIR, EVENT, adapter="codex-session", state="unconfirmed", basis="host_result_unknown")
            self.assertEqual(result["state"], "unconfirmed")
            with self.assertRaises(ValueError):
                dbond.direct_delivery_receipt(PAIR, EVENT, adapter="codex-session", state="acknowledged", basis="host_result_unknown")
        result, _ = self.send([(202, {"id": "fixed-id", "sequence": 8}),
                               (200, {"id": "fixed-id", "sequence": 8, "state": "unconfirmed", "satisfied": False,
                                      "basis": "host_result_unknown", "adapter": "codex-session"})])
        self.assertEqual(result["state"], "unconfirmed")

    def test_sender_receipt_subscription_uses_separate_cursor_and_no_content(self):
        row = {"id": "fixed-id", "from": OWN, "to": PEER, "sequence": 8, "receipt_sequence": 12,
               "state": "acknowledged", "adapter": "codex-session", "basis": "host_acceptance", "recorded_at": "2026-11-13T00:00:00Z"}
        with patch.object(dbond, "Stream") as stream:
            stream.return_value.next_event.side_effect = [row, row, None]
            self.assertEqual(list(dbond.direct_receipts(PAIR, 11)), [row])
            stream.assert_called_once_with({**PAIR, "endpoint": dbond._direct_url(PAIR)}, 11,
                                           stream_suffix="/receipts-stream", sequence_key="receipt_sequence")
            stream.return_value.close.assert_called_once()
        with patch.object(dbond, "Stream") as stream:
            stream.return_value.next_event.return_value = {**row, "text": "Must never reach chat"}
            with self.assertRaisesRegex(ValueError, "metadata"):
                list(dbond.direct_receipts(PAIR))

    def test_authenticated_receipt_sse_checks_receipt_cursor_not_message_sequence(self):
        row = {"id": "fixed-id", "from": OWN, "to": PEER, "sequence": 8, "receipt_sequence": 12,
               "state": "acknowledged", "adapter": "codex-session", "basis": "host_acceptance", "recorded_at": "2026-11-13T00:00:00Z"}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                assert self.path == f"/v1/services/{OWN}/messages/receipts-stream?after=11"
                assert self.headers["Authorization"] == "Bearer fixture"
                self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
                self.wfile.write(f"id: 12\nevent: receipt\ndata: {json.dumps(row)}\n\n".encode())
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            pair = {**PAIR, "endpoint": f"http://127.0.0.1:{server.server_port}/v1/services/{OWN}/events"}
            self.assertEqual(list(dbond.direct_receipts(pair, 11)), [row])
        finally:
            server.shutdown(); server.server_close()


if __name__ == "__main__":
    unittest.main()
