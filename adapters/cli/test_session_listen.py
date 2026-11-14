"""Disposable two-stream host Monitor fixture; never uses a live pairing."""
import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import contextlib
import io
import types
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).parent))
import dbond

SERVICE = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SENDER = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"


class MonitorFixture:
    def __init__(self):
        self.rows = {"dot": [], "direct": []}
        self.paths = []
        self.receipts = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                assert self.path == f"/v1/services/{SERVICE}/messages/receipt"
                assert self.headers.get("Authorization") == "Bearer c2VjcmV0"
                row = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with outer.lock:
                    outer.receipts.append(row)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"receipt": row, "duplicate": False}).encode())

            def do_GET(self):
                assert self.headers.get("Authorization") == "Bearer c2VjcmV0"
                parsed = urlsplit(self.path)
                lane = "direct" if parsed.path.endswith("/messages/stream") else "dot"
                assert parsed.path.endswith(("/messages/stream", "/events/stream"))
                with outer.lock:
                    outer.paths.append(parsed.path)
                after = int(parse_qs(parsed.query)["after"][0])
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                with outer.lock:
                    rows = [row for row in outer.rows[lane] if row["sequence"] > after]
                for row in rows:
                    frame = f'id: {row["sequence"]}\ndata: {json.dumps(row)}\n\n'.encode()
                    self.wfile.write(frame)
                self.wfile.flush()
                time.sleep(0.15)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/v1/services/{SERVICE}/events"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class SessionListenTests(unittest.TestCase):
    def test_exact_monitor_rearm_uses_ready_native_receiver(self):
        sid = "11111111-2222-3333-4444-555555555555"
        pair = {"client": "claude", "session_id": sid}
        attach = unittest.mock.Mock(return_value={"ready": True})
        with patch.object(dbond, "_read_pairing", return_value=pair), \
             patch.object(dbond, "_initiating_claude", return_value=(sid, "Title")), \
             patch.dict(sys.modules, {"claude_registration": types.SimpleNamespace(attach=attach)}), \
             patch.object(dbond.threading, "Thread") as thread, contextlib.redirect_stdout(io.StringIO()) as output:
            dbond.command_session_listen(types.SimpleNamespace(alias="review"))
        attach.assert_called_once_with("review", sid, pair)
        thread.assert_not_called()
        row = json.loads(output.getvalue())
        self.assertEqual(row["state"], "native_receiver_attached")
        self.assertTrue(row["receiver"]["ready"])
        self.assertNotIn("wakeState", row)
        self.assertNotIn("wake_verified", row.values())

    def test_other_or_unavailable_host_preserves_monitor_fallback(self):
        sid = "11111111-2222-3333-4444-555555555555"
        pair = {"client": "claude", "session_id": sid}
        attach = unittest.mock.Mock(return_value={"ready": False, "state": "waiting_host_bootstrap"})
        with patch.dict(sys.modules, {"claude_registration": types.SimpleNamespace(attach=attach)}), \
             patch.object(dbond, "_initiating_claude", return_value=(sid, "Title")):
            self.assertFalse(dbond._migrate_claude_session_listener("review", pair))
        with patch.dict(sys.modules, {"claude_registration": types.SimpleNamespace(attach=attach)}), \
             patch.object(dbond, "_initiating_claude", return_value=("66666666-2222-3333-4444-555555555555", "Other")):
            attach.reset_mock()
            self.assertFalse(dbond._migrate_claude_session_listener("review", pair))
            attach.assert_not_called()

    def test_native_worker_startup_does_not_race_an_old_monitor(self):
        sid = "11111111-2222-3333-4444-555555555555"
        with patch.dict(sys.modules, {"claude_registration": types.SimpleNamespace(attach=lambda *a: {"state": "starting", "ready": False})}), \
             patch.object(dbond, "_initiating_claude", return_value=(sid, "Title")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertTrue(dbond._migrate_claude_session_listener("review", {"client": "claude", "session_id": sid}))
        row = json.loads(output.getvalue())
        self.assertEqual(row["state"], "native_receiver_starting")
        self.assertEqual(row["receiver"], {"state": "starting", "ready": False})
        self.assertEqual(row["verify_command"], "dbond session-status review")
        self.assertNotIn("wakeState", row)
        self.assertNotIn("wake_verified", row.values())

    def test_native_receiver_preserves_exact_unready_state_and_reason(self):
        sid = "11111111-2222-3333-4444-555555555555"
        for state in ("connecting", "blocked", "waiting_existing_listener", "host_offline", "unconfirmed"):
            receiver = {"state": state, "ready": False, "reason": "fixture_reason"}
            attachment = {**receiver, "instruction": "redundant action", "verify_command": "duplicate command"}
            with self.subTest(state=state), \
                 patch.dict(sys.modules, {"claude_registration": types.SimpleNamespace(attach=lambda *a: attachment)}), \
                 patch.object(dbond, "_initiating_claude", return_value=(sid, "Title")), \
                 patch.object(dbond.threading, "Thread") as thread, contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertTrue(dbond._migrate_claude_session_listener("review", {"client": "claude", "session_id": sid}))
            thread.assert_not_called()
            row = json.loads(output.getvalue())
            self.assertEqual(row["state"], "native_receiver_" + state)
            self.assertEqual(row["receiver"], receiver)
            self.assertFalse(row["receiver"]["ready"])
            self.assertNotIn("instruction", row["receiver"])
            self.assertNotIn("verify_command", row["receiver"])
            self.assertEqual(row["verify_command"], "dbond session-status review")
            self.assertNotIn("wakeState", row)
            self.assertNotIn("wake_verified", row.values())

    def test_combined_stream_and_replay_after_absence(self):
        fixture = MonitorFixture()
        try:
            with tempfile.TemporaryDirectory(dir=Path.home()) as folder:
                store = Path(folder) / "pairings"
                store.mkdir(mode=0o700)
                pair = store / "review.json"
                pair.write_text(json.dumps({"endpoint": fixture.endpoint, "secret": "c2VjcmV0",
                                            "destination": "dot", "cursor": 0, "direct_cursor": 0, "pushes": []}))
                pair.chmod(0o600)
                fixture.rows["dot"].append({"id": "@dot:9", "sequence": 1, "state": "dot_push", "reply": "Review?"})
                fixture.rows["direct"].append({"id": "agent-1", "sequence": 1, "from": SENDER,
                                                "to": SERVICE, "text": "Ping", "in_reply_to": None})
                first = self._run_until(store, pair, wanted={"@dot:9", "agent-1"}, cursor=1)
                self.assertEqual({row["state"] for row in first}, {"dot_push", "direct_message"})
                self.assertEqual({row["alias"] for row in first}, {"review"})
                self.assertEqual(next(row for row in first if row["state"] == "direct_message")["sender_id"], SENDER)
                fixture.rows["dot"].append({"id": "@dot:10", "sequence": 2, "state": "dot_push", "reply": "Followup?"})
                fixture.rows["direct"].append({"id": "agent-2", "sequence": 2, "from": SENDER,
                                                "to": SERVICE, "text": "Still there?", "in_reply_to": "agent-1"})
                second = self._run_until(store, pair, wanted={"@dot:10", "agent-2"}, cursor=2)
                self.assertEqual({row["source_id"] for row in second}, {"@dot:10", "agent-2"})
                self.assertEqual(next(row for row in second if row["state"] == "direct_message")["in_reply_to"], "agent-1")
                self.assertEqual([row["id"] for row in fixture.receipts], ["agent-1", "agent-2"])
                self.assertTrue(all(row["state"] == "received" and row["basis"] == "host_acceptance"
                                    and row["adapter"] == "cli-monitor" for row in fixture.receipts))
        finally:
            fixture.close()

    def _run_until(self, store, pair, *, wanted, cursor):
        env = dict(os.environ, HOME=str(store.parent), DBOND_PAIRING_DIR=str(store))
        proc = subprocess.Popen([sys.executable, str(Path(dbond.__file__)), "session-listen", "review"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        rows, pending = [], b""
        deadline = time.monotonic() + 5
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([proc.stdout], [], [], 0.1)
                if ready:
                    pending += os.read(proc.stdout.fileno(), 65536)
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        rows.append(json.loads(line))
                saved = json.loads(pair.read_text())
                needs_dot = saved.get("destination") != "peer"
                if ({row["source_id"] for row in rows} == wanted
                        and (not needs_dot or saved["cursor"] >= cursor) and saved["direct_cursor"] >= cursor):
                    return rows
                if proc.poll() is not None:
                    break
            raise AssertionError(f"listener did not deliver both streams: {rows}, stderr={proc.stderr.read().decode() if proc.poll() is not None else 'still running'}")
        finally:
            proc.terminate()
            proc.wait(timeout=3)
            proc.stdout.close()
            proc.stderr.close()

    def test_invalid_address_is_not_emitted_or_advanced(self):
        with self.assertRaisesRegex(ValueError, "Invalid addressed"):
            dbond._session_row({"id": "agent-1", "sequence": 1, "from": SENDER,
                                "to": "wrong", "text": "secret"}, direct=True, alias="review",
                               service_id=SERVICE, cursor=0)
        self.assertEqual(dbond.session_listener_guidance("review")["command"], "dbond session-listen review")
        self.assertEqual(dbond.session_listener_guidance("review", direct_only=True)["streams"], ["direct"])
        challenge = {"id": "@wake:11111111-2222-3333-4444-555555555555", "sequence": 2,
                     "from": "owner", "to": SERVICE, "text": "Wake check", "in_reply_to": None}
        self.assertEqual(dbond._session_row(challenge, direct=True, alias="review",
                                            service_id=SERVICE, cursor=0)["source_id"], challenge["id"])
        with self.assertRaisesRegex(ValueError, "Invalid addressed"):
            dbond._session_row({**challenge, "from": SENDER}, direct=True, alias="review",
                               service_id=SERVICE, cursor=0)

    def test_direct_only_pairing_uses_one_stream(self):
        fixture = MonitorFixture()
        try:
            with tempfile.TemporaryDirectory(dir=Path.home()) as folder:
                store = Path(folder) / "pairings"
                store.mkdir(mode=0o700)
                pair = store / "review.json"
                pair.write_text(json.dumps({"endpoint": fixture.endpoint, "secret": "c2VjcmV0",
                                            "destination": "peer", "cursor": 0, "direct_cursor": 0}))
                pair.chmod(0o600)
                fixture.rows["direct"].append({"id": "peer-only", "sequence": 1, "from": SENDER,
                                                "to": SERVICE, "text": "Hello", "in_reply_to": None})
                rows = self._run_until(store, pair, wanted={"peer-only"}, cursor=1)
                self.assertEqual(rows[0]["state"], "direct_message")
                self.assertTrue(fixture.paths)
                self.assertTrue(all(path.endswith("/messages/stream") for path in fixture.paths))
        finally:
            fixture.close()


if __name__ == "__main__":
    unittest.main()
