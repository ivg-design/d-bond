#!/usr/bin/env python3
"""Focused local protocol fixture; no D-Bond app, Dot, or other service is contacted."""
import contextlib
import json
import io
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import types
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import dbond


class Fixture:
    def __init__(self, *, ack=True):
        self.ack = ack
        self.get_open = threading.Event()
        self.posts = []
        self.frames = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                assert self.path.endswith("/stream?after=0")
                assert self.headers.get("Authorization") == "Bearer c2VjcmV0"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                outer.get_open.set()
                while not outer.stop.is_set():
                    with outer.lock:
                        if outer.frames:
                            frame = outer.frames.pop(0)
                        else:
                            frame = None
                    if frame is not None:
                        self.wfile.write(frame)
                        self.wfile.flush()
                    else:
                        time.sleep(.01)

            def do_POST(self):
                assert outer.get_open.is_set(), "SSE must be open before POST"
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.posts.append(body)
                ident = body["id"]
                rows = [(1, "dot_stored", "req_0123456789abcdef0123456789abcdef")]
                if outer.ack:
                    rows += [(2, "dot_acknowledged", "Dot explicitly acknowledged"),
                             (3, "dot_replied", "fixture answer")]
                with outer.lock:
                    outer.frames.extend((f"id: {seq}\ndata: ".encode() +
                        json.dumps({"id": ident, "sequence": seq, "state": state, "reply": reply}).encode() + b"\n\n")
                        for seq, state, reply in rows)
                raw = json.dumps({"id": ident, "state": "queued"}).encode()
                self.send_response(202)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/v1/services/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/events"

    def close(self):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()


class DBondCLITest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "Library" / "Application Support" / "D-Bond CLI"
        self.home_patcher = patch.object(dbond.Path, "home", return_value=Path(self.tmp.name))
        self.home_patcher.start()
        self.patcher = patch.object(dbond, "APP_DIR", self.store)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.home_patcher.stop()
        self.tmp.cleanup()

    def link(self, endpoint):
        src = Path(self.tmp.name) / "pair.json"
        src.write_text(json.dumps({"endpoint": endpoint, "secret": "c2VjcmV0"}))
        os.chmod(src, 0o600)
        with contextlib.redirect_stdout(io.StringIO()):
            dbond.command_link(type("Args", (), {"alias": "test", "file": str(src)})())

    def test_link_is_private_and_rejects_pairing_symlink(self):
        self.link("http://127.0.0.1:18768/v1/services/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/events")
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.store / "test.json").stat().st_mode & 0o777, 0o600)
        victim = Path(self.tmp.name) / "victim"
        victim.write_text("not a pairing")
        (self.store / "bad.json").symlink_to(victim)
        with self.assertRaises(SystemExit):
            dbond._read_pairing("bad")

    def test_pairing_dir_rejects_traversal_home_root_and_dangling_parent(self):
        home = Path(self.tmp.name)
        outside = home.parent / (home.name + "-outside")
        lexical_escape = home / ".." / outside.name / "pairings"
        with patch.object(dbond, "APP_DIR", lexical_escape), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                dbond._safe_dir()
        self.assertFalse(outside.exists())
        with patch.object(dbond, "APP_DIR", home), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                dbond._safe_dir()
        dangling = home / "dangling"
        dangling.symlink_to(outside)
        with patch.object(dbond, "APP_DIR", dangling / "pairings"), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                dbond._safe_dir()
        self.assertFalse(outside.exists())
        target_link = home / "target-link"
        target_link.symlink_to(outside)
        with patch.object(dbond, "APP_DIR", target_link), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                dbond._safe_dir()
        self.assertFalse(outside.exists())
        # The preexisting default layout remains available and owner-private.
        with patch.object(dbond, "APP_DIR", home / "Library/Application Support/D-Bond CLI"):
            self.assertEqual(dbond._safe_dir().stat().st_mode & 0o777, 0o700)

    def test_trace_time_formats_fractional_and_legacy_iso_in_local_zone(self):
        fractional = dbond._local_trace_time("2026-11-12T16:40:11.068+00:00")
        legacy = dbond._local_trace_time("2026-11-12T16:40:11+00:00")
        self.assertIn("2026", fractional)
        self.assertIn("2026", legacy)
        self.assertNotIn(".068", fractional)
        self.assertNotEqual(fractional, "2026-11-12T16:40:11.068+00:00")

    def test_trace_time_respects_daylight_saving_transition(self):
        old_tz = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "America/New_York"
            time.tzset()
            before = dbond._local_trace_time("2026-03-08T06:59:59Z")
            after = dbond._local_trace_time("2026-03-08T07:00:00Z")
            self.assertEqual(before, "Mar 8, 2026, 1:59:59 AM EST")
            self.assertEqual(after, "Mar 8, 2026, 3:00:00 AM EDT")
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()

    def test_format_trace_keeps_raw_trace_unmodified(self):
        raw = {"event": {"stages": [{"at": "2026-11-12T16:40:11.068+00:00", "state": "queued"}]}}
        formatted = dbond._format_trace(raw)
        self.assertNotIn(".068", formatted["event"]["stages"][0]["at"])
        self.assertIn(".068", raw["event"]["stages"][0]["at"])

    def test_trace_command_reads_owner_socket_and_prints_formatted_stages(self):
        raw = {"events": [{"stages": [{"at": "2026-11-12T16:40:11.068+00:00"}]}]}
        output = io.StringIO()
        with patch.object(dbond, "_owner_request", return_value=raw) as request, contextlib.redirect_stdout(output):
            dbond.command_trace(type("Args", (), {"connection_id": "11111111-2222-3333-4444-555555555555",
                                                       "event_id": None, "limit": 1})())
        request.assert_called_once_with({"op": "list_events", "id": "11111111-2222-3333-4444-555555555555", "limit": 1})
        self.assertNotIn(".068", output.getvalue())

    def test_trace_command_selects_ambiguous_direct_event(self):
        connection = "11111111-2222-3333-4444-555555555555"
        counterpart = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        args = type("Args", (), {"connection_id":connection,"event_id":"same-id","limit":20,
                                 "direction":"inbound","counterpart_id":counterpart})()
        with patch.object(dbond,"_owner_request",return_value={"event":None}) as request, contextlib.redirect_stdout(io.StringIO()):
            dbond.command_trace(args)
        request.assert_called_once_with({"op":"trace","id":connection,"eventID":"same-id",
                                         "direction":"inbound","counterpartID":counterpart})

    def test_stale_listener_writes_merge_cursor_and_pushes_under_lock(self):
        self.link("http://127.0.0.1:18768/v1/services/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/events")
        one = dbond._read_pairing("test")
        two = dbond._read_pairing("test")
        one.update(cursor=7, pushes=["@dot:7"])
        two.update(cursor=9, pushes=["@dot:9"])
        dbond._save("test", one)
        dbond._save("test", two)
        merged = dbond._read_pairing("test")
        self.assertEqual(merged["cursor"], 9)
        self.assertEqual(set(merged["pushes"]), {"@dot:7", "@dot:9"})

    def test_session_status_distinguishes_pairing_from_active_streams(self):
        endpoint = "http://127.0.0.1:18768/v1/services/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/events"
        self.link(endpoint)
        service = {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "destination": "dot",
                   "paused": False, "directSubscribers": 0, "subscribers": 1,
                   "wakeState": "pending", "lastVerifiedAt": "2026-11-12T20:00:00Z"}
        with patch.object(dbond, "_owner_request", return_value={"services": [service]}):
            first = dbond.session_status("test")
            self.assertEqual(first["state"], "pending_listener")
            self.assertEqual(first["wakeState"], "pending")
            self.assertEqual(first["lastWakeVerifiedAt"], "2026-11-12T20:00:00Z")
            self.assertIn("2026", first["lastWakeVerifiedLocal"])
            self.assertNotIn("T20:00:00Z", first["lastWakeVerifiedLocal"])
            service["directSubscribers"] = 1
            service["wakeState"] = "verified"
            self.assertEqual(dbond.session_status("test")["state"], "subscriber_present")
            self.assertEqual(dbond.session_status("test")["wakeState"], "verified")
            service["paused"] = True
            self.assertEqual(dbond.session_status("test")["state"], "paused")
        with patch.object(dbond, "_owner_request", side_effect=OSError):
            self.assertEqual(dbond.session_status("test")["state"], "unknown")

    def test_registration_finish_is_explicit_and_idempotent_at_owner_boundary(self):
        endpoint = "http://127.0.0.1:18768/v1/services/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/events"
        self.link(endpoint)
        service = {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "destination": "dot",
                   "paused": False, "directSubscribers": 1, "subscribers": 1,
                   "wakeState": "pending", "lastVerifiedAt": None}
        calls = []
        def owner(payload):
            calls.append(payload)
            if payload["op"] == "list":
                return {"services": [service]}
            return {"id": service["id"], "wakeID": "@wake:fixture", "wakeState": "pending", "scheduled": True}
        with patch.object(dbond, "_owner_request", side_effect=owner):
            self.assertNotIn("registration_finish", dbond.session_status("test"))
            self.assertEqual(calls, [{"op": "list"}])
            first = dbond.session_status("test", finish_registration=True)
            second = dbond.session_status("test", finish_registration=True)
            self.assertEqual(first["registration_finish"], second["registration_finish"])
            self.assertEqual(first["registration_finish"], {"scheduled": True, "wakeState": "pending", "state": "scheduled"})
            self.assertEqual([row["op"] for row in calls], ["list", "list", "finish_registration", "list", "finish_registration"])
            service["directSubscribers"] = 0
            pending = dbond.session_status("test", finish_registration=True)
            self.assertEqual(pending["registration_finish"]["state"], "listener_not_ready")
            self.assertEqual(calls[-1], {"op": "list"})
        with self.assertRaisesRegex(ValueError, "boolean"):
            dbond.session_status("test", finish_registration="yes")

    def test_exact_codex_session_alias_reuses_pairing_and_attaches_once(self):
        session_id = "11111111-2222-3333-4444-555555555555"
        alias = f"codex-{session_id}"
        endpoint = "http://127.0.0.1:18768/v1/services/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/events"
        self.link(endpoint)
        (self.store / f"{alias}.json").write_bytes((self.store / "test.json").read_bytes())
        os.chmod(self.store / f"{alias}.json", 0o600)
        args = type("Args", (), {"alias": alias, "client": "codex", "session_id": session_id,
                                  "title": "Review", "name": None, "direct": False})()
        attached = []
        fake_module = types.SimpleNamespace(attach=lambda a, sid, pair: attached.append((a, sid)) or {"state": "attached"})
        with patch.dict(sys.modules, {"codex_registration": fake_module}), \
             patch.object(dbond, "_peers", return_value=[{"id":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "name":"Codex / Review"}]), \
             patch.object(dbond, "session_status", return_value={"wakeState":"offline"}), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            dbond.command_enroll(args)
        result = json.loads(out.getvalue())
        self.assertTrue(result["reused"])
        self.assertEqual(result["wake"]["state"], "offline")
        self.assertEqual(result["listener"]["state"], "attached")
        self.assertEqual(attached, [(alias, session_id)])
        self.assertEqual(dbond._read_pairing(alias)["session_id"], session_id)

    def test_codex_enrollment_arms_finish_only_after_ready_listener(self):
        session_id = "11111111-2222-3333-4444-555555555555"
        alias = f"codex-{session_id}"
        endpoint = "http://127.0.0.1:18768/v1/services/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/events"
        self.link(endpoint)
        dbond._save(alias, dbond._read_pairing("test"))
        args = type("Args", (), {"alias": alias, "client": "codex", "session_id": session_id,
                                  "title": "Review", "name": None, "direct": False})()
        with patch.object(dbond, "_peers", return_value=[{"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "name": "Codex / Review"}]), \
             patch.object(dbond, "_enrollment_listener", return_value={"state": "listening", "ready": True}), \
             patch.object(dbond, "session_status", return_value={"registration_finish": {"scheduled": True}}) as finish, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            dbond.command_enroll(args)
        self.assertTrue(json.loads(out.getvalue())["registration_finish"]["scheduled"])
        finish.assert_called_once_with(alias, finish_registration=True)
        with patch.object(dbond, "_peers", return_value=[{"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "name": "Codex / Review"}]), \
             patch.object(dbond, "_enrollment_listener", return_value={"state": "degraded", "ready": False}), \
             patch.object(dbond, "session_status", return_value={"wakeState": "offline"}) as no_finish, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            dbond.command_enroll(args)
        self.assertNotIn("registration_finish", json.loads(out.getvalue()))
        no_finish.assert_called_once_with(alias, finish_registration=False)

    def test_acknowledgement_sends_only_received_sequence(self):
        import wake_registration
        self.link("http://127.0.0.1:18768/v1/services/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/events")
        response = {"state": "verified", "sequence": 8, "id": "@wake:11111111-2222-3333-4444-555555555555", "duplicate": False}
        with patch.dict(sys.modules, {"dbond": dbond}), patch.object(dbond, "_request", return_value=(200, response)) as request:
            receipt = wake_registration.acknowledge("test", 8)
            self.assertEqual(receipt["sequence"], 8)
            self.assertEqual(receipt["id"], response["id"])
            self.assertFalse(receipt["duplicate"])
            self.assertEqual(request.call_args.args[3], {"sequence": 8})
            for invalid in (True, 0, -1, "8", 2**63):
                with self.assertRaises(ValueError):
                    wake_registration.acknowledge("test", invalid)
            legacy_value = "obsolete-value"
            with contextlib.redirect_stderr(io.StringIO()) as error, self.assertRaises(SystemExit):
                dbond.parser().parse_args(["wake-ack", "test", legacy_value])
            self.assertNotIn(legacy_value, error.getvalue())
            self.assertEqual(request.call_count, 1)
            request.return_value = (200, {**response, "sequence": 9})
            with self.assertRaises(ValueError):
                wake_registration.acknowledge("test", 8)

    def test_exact_session_binding_lookup_is_unique_and_private(self):
        endpoint = "http://127.0.0.1:18768/v1/services/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/events"
        self.link(endpoint)
        session_id = "11111111-2222-3333-4444-555555555555"
        self.assertEqual(dbond.bind_session("test", "claude", session_id)["state"], "bound")
        self.assertEqual(dbond.session_binding("claude", session_id), {
            "alias": "test", "service_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"})
        stale = {"endpoint": endpoint, "secret": "c2VjcmV0", "cursor": 2}
        dbond._save("test", stale)
        self.assertEqual(dbond._read_pairing("test")["session_id"], session_id)
        with self.assertRaisesRegex(ValueError, "another host session"):
            dbond.bind_session("test", "claude", "66666666-2222-3333-4444-555555555555")
        second = self.store / "second.json"
        second.write_bytes((self.store / "test.json").read_bytes())
        os.chmod(second, 0o600)
        with self.assertRaisesRegex(ValueError, "No unique"):
            dbond.session_binding("claude", session_id)

    def test_enroll_uses_owner_socket_and_never_prints_secret(self):
        sock_path = Path(self.tmp.name) / "pair.sock"
        server = __import__("socket").socket(__import__("socket").AF_UNIX)
        server.bind(str(sock_path)); os.chmod(sock_path, 0o600); server.listen(1)
        endpoint = "http://127.0.0.1:18768/v1/services/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/events"
        response = {"id":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "secret":"c2VjcmV0",
                    "endpoint":endpoint, "destination":"dot", "herald":False, "chromeOrigin":None}
        def serve():
            conn, _ = server.accept()
            data = b""
            while not data.endswith(b"\n"):
                data += conn.recv(1024)
            request = json.loads(data)
            self.assertEqual(request, {"op":"pair", "name":"Claude / Live test"})
            conn.sendall(json.dumps(response).encode() + b"\n")
            conn.close()
        thread = threading.Thread(target=serve); thread.start()
        out, err = io.StringIO(), io.StringIO()
        try:
            with patch.object(dbond, "OWNER_SOCKET", sock_path), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                dbond.command_enroll(type("Args", (), {"alias":"new", "name":"Claude / Live test"})())
            thread.join(2)
            result = json.loads(out.getvalue())
            self.assertEqual(result["linked"], "new")
            self.assertEqual(result["endpoint"], endpoint)
            self.assertEqual(result["listener"]["command"], "dbond session-listen new")
            self.assertEqual(result["listener"]["streams"], ["direct", "dot"])
            self.assertIn("saved cursors replay", result["listener"]["instruction"])
            self.assertNotIn("c2VjcmV0", out.getvalue())
            self.assertNotIn("c2VjcmV0", err.getvalue())
            self.assertEqual(dbond._read_pairing("new")["secret"], "c2VjcmV0")
            with self.assertRaises(SystemExit):
                with patch.object(dbond, "OWNER_SOCKET", sock_path):
                    dbond.command_enroll(type("Args", (), {"alias":"new", "name":"render"})())
        finally:
            server.close()

    def test_herald_enrollment_reuses_durable_alias_without_session_setup(self):
        args = dbond.parser().parse_args(["enroll", "herald-notifications", "--service", "Herald",
                                          "--topic", "Notifications", "--destination", "herald"])
        self.link("http://127.0.0.1:18768/v1/services/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/events")
        pair = dict(dbond._read_pairing("test"), destination="herald")
        dbond._save(args.alias, pair)
        service_id = pair["endpoint"].split("/")[-2]
        peers = [{"id":service_id, "name":"Herald / Notifications", "paused":False}]
        with patch.object(dbond, "_peers", return_value=peers), contextlib.redirect_stdout(io.StringIO()) as output:
            dbond.command_enroll(args)
        result = json.loads(output.getvalue())
        self.assertTrue(result["reused"])
        self.assertEqual(result["destination"], "herald")
        self.assertNotIn("listener", result)
        self.assertNotIn("secret", result)

    def test_claude_title_uses_exact_session_metadata_and_custom_title_precedence(self):
        home = Path(self.tmp.name)
        session_id = "3f47c4fa-1111-4222-8333-444455556666"
        transcript = home / ".claude" / "projects" / "project-a" / f"{session_id}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("\n".join([
            json.dumps({"type":"user", "message":{"content":"private prompt must not become a title"}}),
            json.dumps({"type":"ai-title", "aiTitle":"Generated title"}),
            json.dumps({"type":"custom-title", "customTitle":"Human title"}),
        ]))
        sidecar_dir = home / "Library/Application Support/Claude/claude-code-sessions/a/b"
        sidecar_dir.mkdir(parents=True)
        (sidecar_dir / "local_opaque.json").write_text(json.dumps({
            "cliSessionId":session_id, "title":"Desktop title"}))
        self.assertEqual(dbond._claude_session_title(session_id, home), "Human title")
        self.assertEqual(dbond._claude_desktop_title(session_id, home), "Desktop title")
        self.assertEqual(dbond._claude_session_title("3f47c4fa-9999-4222-8333-444455556666", home), None)

    def test_enrollment_name_requires_exact_id_and_never_uses_prompt_text(self):
        session_id = "3f47c4fa-1111-4222-8333-444455556666"
        args = type("Args", (), {"name":"alias", "client":"codex", "session_id":session_id,
                                 "title":None})()
        self.assertEqual(dbond.enrollment_name(args), "Codex / alias")
        args.session_id = "nearby-session"
        with self.assertRaises(SystemExit):
            dbond.enrollment_name(args)

    def test_ask_waits_for_explicit_ack_and_reply_after_stream_connects(self):
        fixture = Fixture(ack=True)
        try:
            self.link(fixture.endpoint)
            out = io.StringIO()
            args = type("Args", (), {"alias": "test", "event_id": None,
                "question": "hello", "timeout": 3})()
            with contextlib.redirect_stdout(out):
                dbond.command_ask(args)
            result = json.loads(out.getvalue())
            self.assertEqual(result["state"], "dot_replied")
            self.assertEqual(result["request_id"], "req_0123456789abcdef0123456789abcdef")
            self.assertEqual(result["ack"], "Dot explicitly acknowledged")
            self.assertEqual(fixture.posts[0]["payload"], {"text": "hello"})
        finally:
            fixture.close()

    def test_stored_receipt_times_out_without_being_called_ack(self):
        fixture = Fixture(ack=False)
        try:
            self.link(fixture.endpoint)
            out = io.StringIO()
            args = type("Args", (), {"alias": "test", "event_id": "stable-event",
                "question": "hello", "timeout": 1})()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as raised:
                dbond.command_ask(args)
            self.assertEqual(raised.exception.code, 3)
            result = json.loads(out.getvalue())
            self.assertEqual(result["state"], "dot_stored")
            self.assertEqual(result["request_id"], "req_0123456789abcdef0123456789abcdef")
        finally:
            fixture.close()

    def test_answer_uses_only_recorded_dot_push_and_expected_payload(self):
        fixture = Fixture(ack=False)
        try:
            self.link(fixture.endpoint)
            pairing = dbond._read_pairing("test")
            pairing["pushes"] = ["@dot:91"]
            dbond._save("test", pairing)
            with self.assertRaises(SystemExit):
                dbond.command_answer(type("Args", (), {"alias":"test", "dot_id":"92", "text":"no"})())
            out = io.StringIO()
            args = type("Args", (), {"alias":"test", "dot_id":"91", "text":"yes", "timeout":1})()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as raised:
                dbond.command_answer(args)
            self.assertEqual(raised.exception.code, 3)
            result = json.loads(out.getvalue())
            self.assertEqual(result["state"], "dot_stored")
            self.assertEqual(result["request_id"], "req_0123456789abcdef0123456789abcdef")
            self.assertEqual(fixture.posts[-1]["payload"], {
                "kind":"service.reply", "in_reply_to":"@dot:91", "text":"yes"})
        finally:
            fixture.close()


if __name__ == "__main__":
    unittest.main()
