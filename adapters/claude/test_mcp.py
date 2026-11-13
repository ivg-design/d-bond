"""Exercise the packed bundle's actual stdio entry point without Dot sends."""
import json
import os
import pathlib
import selectors
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "dist/d-bond-claude.mcpb"


def exchange(channel=False, interpreter=None, native=False):
    with tempfile.TemporaryDirectory() as folder:
        temp = pathlib.Path(folder)
        with zipfile.ZipFile(BUNDLE) as bundle:
            bundle.extractall(temp)
        env = dict(os.environ, HOME=str(temp), PATH="/usr/bin:/bin")
        server = None
        service_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        sender_id = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
        if channel:
            env.update(DBOND_CLAUDE_CHANNEL="1", DBOND_CLAUDE_ALIAS="claude-document-review")
        class Endpoint(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if self.path.endswith("after=0"):
                    assert self.headers.get("Authorization") == "Bearer c2VjcmV0"
                    if "/messages/stream" in self.path:
                        row = {"id": "direct-9", "sequence": 2, "from": sender_id, "to": service_id,
                               "text": "What changed?", "in_reply_to": None}
                        self.wfile.write(f"id: 2\ndata: {json.dumps(row)}\n\n".encode())
                    else:
                        row = {"id": "@dot:9", "sequence": 1, "state": "dot_push", "reply": "Review the design?"}
                        self.wfile.write(f"id: 1\ndata: {json.dumps(row)}\n\n".encode())
                    self.wfile.flush()
                time.sleep(0.2)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        store = temp / "Library/Application Support/D-Bond CLI"
        store.mkdir(parents=True, mode=0o700)
        pairing = store / "claude-document-review.json"
        pairing.write_text(json.dumps({"endpoint": f"http://127.0.0.1:{server.server_port}/v1/services/{service_id}/events",
                                       "secret": "c2VjcmV0", "cursor": 0, "direct_cursor": 0, "pushes": []}))
        pairing.chmod(0o600)
        entry = [interpreter, str(temp / "server/mcp.py")] if interpreter else ["/bin/sh", str(temp / "server/launch.sh")]
        if native:
            launcher = temp / "server/d-bond-launcher"
            launcher.chmod(0o755)  # zipfile extraction does not restore POSIX modes
            entry = [str(launcher)]
        proc = subprocess.Popen(entry,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env)
        notifications = []
        def request(ident, method, params=None):
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": ident, "method": method,
                                         "params": params or {}}) + "\n")
            proc.stdin.flush()
            while True:
                row = json.loads(proc.stdout.readline())
                if row.get("id") == ident:
                    return row
                notifications.append(row)
        try:
            init = request(1, "initialize", {"protocolVersion": "2025-06-18",
                                               "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}})
            assert init["result"]["serverInfo"]["name"] == "d-bond"
            assert ("claude/channel" in init["result"]["capabilities"].get("experimental", {})) == channel
            proc.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            proc.stdin.flush()
            tools = request(2, "tools/list")["result"]["tools"]
            expected = {"enroll", "peers", "ask", "answer", "direct_peers", "direct_send", "direct_answer"}
            if not channel:
                expected.update(("wait_for_push", "wait_for_direct"))
            assert {t["name"] for t in tools} == expected
            if channel:
                assert "alias" not in tools[0]["inputSchema"]["properties"]
            bad = request(3, "tools/call", {"name": "peers", "arguments": {"alias": "missing"}})
            assert bad["result"]["isError"]
            if channel:
                while len([row for row in notifications if row.get("method") == "notifications/claude/channel"]) < 2:
                    with selectors.DefaultSelector() as ready:
                        ready.register(proc.stdout, selectors.EVENT_READ)
                        assert ready.select(5), "channel did not emit its addressed push"
                    notifications.append(json.loads(proc.stdout.readline()))
                push = next(row for row in notifications if row.get("params", {}).get("meta", {}).get("source_id") == "@dot:9")
                assert push["params"] == {"content": "Review the design?",
                                          "meta": {"source_id": "@dot:9", "sequence": "1"}}
                direct = next(row for row in notifications if row.get("params", {}).get("meta", {}).get("source_id") == "direct-9")
                assert direct["params"] == {"content": "What changed?", "meta": {
                    "source": "d-bond-direct", "source_id": "direct-9", "sender_id": sender_id, "sequence": "2"}}
                until=time.monotonic()+2
                while time.monotonic()<until:
                    saved = json.loads(pairing.read_text())
                    if saved.get("cursor")==1 and saved.get("direct_cursor")==2: break
                    time.sleep(.02)
                assert saved["cursor"] == 1 and "@dot:9" in saved["pushes"] and saved["direct_cursor"] == 2
            else:
                waited = request(4, "tools/call", {"name": "wait_for_direct", "arguments": {
                    "alias": "claude-document-review", "timeout": 2}})
                result = json.loads(waited["result"]["content"][0]["text"])
                assert result == {"state": "direct_message", "id": "direct-9", "sender_id": sender_id,
                                  "text": "What changed?", "in_reply_to": None, "sequence": 2}
                until=time.monotonic()+2
                while json.loads(pairing.read_text()).get("direct_cursor") != 2 and time.monotonic()<until:
                    time.sleep(.02)
                assert json.loads(pairing.read_text())["direct_cursor"] == 2
        finally:
            proc.stdin.close()
            proc.wait(timeout=5)
            assert proc.returncode == 0, proc.stderr.read()
            if server:
                server.shutdown()
                server.server_close()


def enrollment_reuse():
    sys.path.insert(0, str(ROOT / "adapters/claude/plugin/server"))
    import mcp
    with tempfile.TemporaryDirectory() as folder:
        home = pathlib.Path(folder)
        store = home / "Library/Application Support/D-Bond CLI"
        service_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        endpoint = f"http://127.0.0.1:7777/v1/services/{service_id}/events"
        sent = []
        def fake_cli(*args):
            sent.append(args)
            store.mkdir(parents=True, exist_ok=True, mode=0o700)
            pairing = store / f"{args[1]}.json"
            pairing.write_text(json.dumps({"endpoint": endpoint, "secret": "c2VjcmV0"}))
            pairing.chmod(0o600)
            return {"linked": args[1], "endpoint": endpoint}
        with mock.patch.object(pathlib.Path, "home", return_value=home), \
             mock.patch.object(mcp.dbond, "APP_DIR", store), \
             mock.patch.object(mcp, "cli", side_effect=fake_cli), \
             mock.patch.object(mcp.dbond, "_peers", return_value=[{"id": service_id, "name": "Claude / Design review"}]):
            first = mcp.enroll({"topic": "Design review"})
            second = mcp.enroll({"topic": "Design review"})
            assert not first["reused"] and second["reused"]
            assert first["alias"] == second["alias"] and len(sent) == 1
            assert sent[0][3] == "Claude / Design review"


def direct_tool_commands():
    sys.path.insert(0, str(ROOT / "adapters/claude/plugin/server"))
    import mcp
    commands=[]
    with mock.patch.object(mcp, "cli", side_effect=lambda *args: commands.append(args) or {"stored": True}):
        mcp.call_tool("direct_peers", {"alias": "fixture"})
        mcp.call_tool("direct_send", {"alias": "fixture", "to": "Claude / Review", "text": "Question", "message_id": "direct-q-1"})
        mcp.call_tool("direct_answer", {"alias": "fixture", "sender_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                                         "source_id": "direct-q-1", "text": "Answer", "message_id": "direct-r-1"})
    assert commands == [("direct-peers", "fixture"),
                        ("direct-send", "fixture", "Claude / Review", "Question", "--message-id", "direct-q-1"),
                        ("direct-answer", "fixture", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "direct-q-1", "Answer", "--message-id", "direct-r-1")]
    pairing = {"endpoint": "http://127.0.0.1:18768/v1/services/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/events"}
    owner_note = {"id": "owner-setup-1", "sequence": 3, "from": "owner",
                  "to": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "text": "Setup note"}
    assert mcp.valid_direct(owner_note, pairing)["sender_id"] == "owner"
    with mock.patch.object(mcp, "cli") as sender:
        try:
            mcp.direct_send("fixture", "owner", "Unsafe answer", source_id="owner-setup-1")
        except ValueError as exc:
            assert "paired service" in str(exc)
        else:
            raise AssertionError("owner note incorrectly accepted as an answer target")
        sender.assert_not_called()


if __name__ == "__main__":
    exchange()
    exchange(True)
    exchange(interpreter="/usr/bin/python3")
    exchange(True, interpreter="/usr/bin/python3")
    exchange(native=True)
    enrollment_reuse()
    direct_tool_commands()
    print("PASS: packed MCP entry points, Dot/direct channel pushes, manual direct wait, correlated tool commands, and pairing reuse")
