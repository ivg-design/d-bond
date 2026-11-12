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
            self.assertEqual(json.loads(out.getvalue()), {"linked":"new", "endpoint":endpoint})
            self.assertNotIn("c2VjcmV0", err.getvalue())
            self.assertIn("dbond listen new --push-only", err.getvalue())
            self.assertEqual(dbond._read_pairing("new")["secret"], "c2VjcmV0")
            with self.assertRaises(SystemExit):
                with patch.object(dbond, "OWNER_SOCKET", sock_path):
                    dbond.command_enroll(type("Args", (), {"alias":"new", "name":"render"})())
        finally:
            server.close()

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
