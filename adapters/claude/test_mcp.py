"""Exercise the packed bundle's actual stdio entry point without Dot sends."""
import json
import importlib.util
import os
import pathlib
import selectors
import subprocess
import sys
import tempfile
import threading
import time
import uuid
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
        host_session_id = "11111111-2222-3333-4444-555555555555"
        if channel:
            env.update(DBOND_CLAUDE_CHANNEL="1", DBOND_CLAUDE_ALIAS="claude-document-review")
        receipts = []
        class Endpoint(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                assert self.path == f"/v1/services/{service_id}/messages/receipt"
                assert self.headers.get("Authorization") == "Bearer c2VjcmV0"
                row = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                receipts.append(row)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"receipt": row, "duplicate": False}).encode())
            def do_GET(self):
                if self.path.endswith("/peers"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"services": [{"id": service_id, "name": "Claude / Document review"}]}).encode())
                    return
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
                                               "capabilities": {"experimental": {"claude/channel": {}}} if channel else {},
                                               "clientInfo": {"name": "fixture", "version": "1"}})
            assert init["result"]["serverInfo"]["name"] == "d-bond"
            assert "THIS turn" in init["result"]["instructions"]
            assert "LATER recipient turn" in init["result"]["instructions"]
            assert ("claude/channel" in init["result"]["capabilities"].get("experimental", {})) == channel
            proc.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            proc.stdin.flush()
            tools = request(2, "tools/list")["result"]["tools"]
            expected = {"local_status", "enroll", "session_status", "acknowledge_wake",
                        "peers", "ask", "answer", "direct_peers", "direct_send", "direct_answer", "acknowledge_direct_message", "direct_status"}
            if not channel:
                expected.update(("wait_for_push", "wait_for_direct"))
            assert {t["name"] for t in tools} == expected
            assert "THIS registration turn" in next(t for t in tools if t["name"] == "enroll")["description"]
            status_tool = next(t for t in tools if t["name"] == "session_status")
            assert status_tool["inputSchema"]["properties"]["finish_registration"]["type"] == "boolean"
            status = request(30, "tools/call", {"name": "local_status", "arguments": {}})
            value = json.loads(status["result"]["content"][0]["text"])
            assert value["installation"] in {"installed", "uninstalled", "unknown"}, value
            assert value["service"] in {"reachable", "offline", "unknown"}, value
            assert set(value) == {"installation", "service"}, value
            if channel:
                assert "alias" not in tools[0]["inputSchema"]["properties"]
                assert "alias" in next(t for t in tools if t["name"] == "enroll")["inputSchema"]["properties"]
            bad = request(3, "tools/call", {"name": "peers", "arguments": {"alias": "missing"}})
            assert bad["result"]["isError"]
            if channel:
                # A legacy configured alias must not start either stream before
                # this exact host session is explicitly enrolled.
                assert not any(row.get("method") == "notifications/claude/channel" for row in notifications)
                enrolled = request(31, "tools/call", {"name": "enroll", "arguments": {
                    "topic": "Document review", "alias": "claude-document-review", "session_id": host_session_id}})
                assert not enrolled["result"].get("isError"), enrolled
                value = json.loads(enrolled["result"]["content"][0]["text"])
                assert value["binding"]["session_id"] == host_session_id
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
                assert receipts == [{"from": sender_id, "id": "direct-9", "sequence": 2,
                                      "state": "received", "basis": "host_acceptance", "adapter": "claude-channel"}]
                ack = request(32, "tools/call", {"name": "acknowledge_direct_message", "arguments": {
                    "sender_id": sender_id, "source_id": "direct-9", "sequence": 2}})
                assert not ack["result"].get("isError"), ack
                assert receipts[-1]["state"] == "acknowledged" and receipts[-1]["basis"] == "participant"
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
                until=time.monotonic()+2
                while not receipts and time.monotonic()<until: time.sleep(.02)
                assert receipts[-1]["state"] == "acknowledged" and receipts[-1]["basis"] == "participant"
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
             mock.patch.object(mcp.dbond, "session_status", return_value={"state":"pending_listener","wakeState":"offline"}), \
             mock.patch.object(mcp.dbond, "_peers", return_value=[{"id": service_id, "name": "Claude / Design review"}]):
            first = mcp.enroll({"topic": "Design review"})
            second = mcp.enroll({"topic": "Design review"})
            assert not first["reused"] and second["reused"]
            assert first["alias"] == second["alias"] and len(sent) == 1
            assert sent[0][3] == "Claude / Design review"
            assert first["activation"]["state"] == "host_monitor_required"
            assert second["listener"]["command"] == f"dbond session-listen {second['alias']}"
            assert second["session_status"]["state"] == "pending_listener"
            assert second["wake"]["state"] == "offline"
            third = mcp.enroll({"topic": "Renamed title", "alias": first["alias"],
                                "session_id": "11111111-2222-3333-4444-555555555555"})
            assert third["reused"] and third["reused_existing_alias"]
            assert third["display_name"] == "Claude / Design review"
            assert third["binding"]["state"] == "bound"
            fourth = mcp.enroll({"alias": first["alias"], "session_id": "11111111-2222-3333-4444-555555555555"})
            assert fourth["reused"] and fourth["display_name"] == "Claude / Design review"
            fifth = mcp.enroll({"topic": "Guessed from folder",
                                "session_id": "11111111-2222-3333-4444-555555555555"})
            assert fifth["reused"] and fifth["reused_existing_binding"]
            assert fifth["alias"] == first["alias"]
            assert len(sent) == 1
            with mock.patch.object(mcp.dbond, "_initiating_claude", return_value=None), \
                 mock.patch.object(mcp.dbond, "_claude_desktop_title", return_value="Session registration in dbond"):
                sixth = mcp.enroll({"topic": "Guessed from folder",
                                    "session_id": "77777777-2222-3333-4444-555555555555"})
            assert sixth["display_name"] == "Claude / Session registration in dbond"
            assert sent[-1][3] == "Claude / Session registration in dbond"


def concurrent_code_sessions_same_title():
    """Two running channel instances with one title never share a pairing."""
    sys.path.insert(0, str(ROOT / "adapters/claude/plugin/server"))
    source = ROOT / "adapters/claude/server/mcp.py"
    manifest = json.loads((ROOT / "adapters/claude/plugin/.claude-plugin/plugin.json").read_text())
    channel_mcp = json.loads((ROOT / "adapters/claude/plugin/.mcp.json").read_text())
    assert isinstance(manifest["version"], str) and manifest["version"] and "userConfig" not in manifest
    assert channel_mcp["mcpServers"]["d-bond"]["env"] == {"DBOND_CLAUDE_CHANNEL": "1"}
    def instance(name):
        spec = importlib.util.spec_from_file_location(name, source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.CHANNEL = True
        return module
    first, second, third = (instance("claude_session_first"), instance("claude_session_second"),
                            instance("claude_session_third"))
    sid1 = "11111111-2222-3333-4444-555555555555"
    sid2 = "66666666-7777-8888-9999-aaaaaaaaaaaa"
    with tempfile.TemporaryDirectory() as folder:
        store = pathlib.Path(folder) / "pairings"
        store.mkdir(mode=0o700)
        services = []
        def fake_cli(*args):
            assert args[0] == "enroll"
            alias, display = args[1], args[3]
            if any(row["name"].casefold() == display.casefold() for row in services):
                raise ValueError("D-Bond refused enrollment. Use a new display name or inspect the app.")
            service_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, alias))
            endpoint = f"http://127.0.0.1:7777/v1/services/{service_id}/events"
            path = store / f"{alias}.json"
            path.write_text(json.dumps({"endpoint": endpoint, "secret": "c2VjcmV0"}))
            path.chmod(0o600)
            services.append({"id": service_id, "name": display})
            return {"endpoint": endpoint}
        with mock.patch.object(pathlib.Path, "home", return_value=pathlib.Path(folder)), \
             mock.patch.object(first.dbond, "APP_DIR", store), \
             mock.patch.object(first.dbond, "_initiating_claude", return_value=None), \
             mock.patch.object(first.dbond, "_peers", side_effect=lambda _: services), \
             mock.patch.object(first.dbond, "_owner_request", side_effect=lambda _: {"services": services}), \
             mock.patch.object(first.dbond, "session_status", return_value={"state": "pending_listener"}), \
             mock.patch.object(first, "cli", side_effect=fake_cli), \
             mock.patch.object(second, "cli", side_effect=fake_cli):
            assert first.channel_alias() is None and second.channel_alias() is None
            one = first.enroll({"topic": "Identical title", "session_id": sid1})
            two = second.enroll({"topic": "Identical title", "session_id": sid2})
            assert one["alias"] == f"claude-{sid1}" and two["alias"] == f"claude-{sid2}"
            assert one["alias"] != two["alias"] and one["display_name"] != two["display_name"]
            assert one["display_name"] == "Claude / Identical title"
            assert two["display_name"] == "Claude / Identical title [66666666]"
            assert first.channel_alias() == one["alias"] and second.channel_alias() == two["alias"]
            assert first.enroll({"topic": "Renamed", "session_id": sid1})["alias"] == one["alias"]
            with mock.patch.object(second.dbond, "_initiating_claude", return_value=(sid2, "Identical title")):
                second.CHANNEL_SESSION_ID = None
                assert second.initialize_channel_identity() == two["alias"]
            try:
                second.call_tool("peers", {"alias": one["alias"]})
            except ValueError as exc:
                assert "different exact host session" in str(exc)
            else:
                raise AssertionError("Second Code session selected first session's pairing")
            try:
                first.enroll({"topic": "Other", "session_id": sid2})
            except ValueError as exc:
                assert "another Claude Code session" in str(exc)
            else:
                raise AssertionError("Running channel changed host identity")
            with mock.patch.object(third, "cli", side_effect=ValueError(
                    "D-Bond refused enrollment. Use a new display name or inspect the app.")) as failed:
                try:
                    third.enroll({"topic": "Unavailable", "session_id": "99999999-aaaa-bbbb-cccc-dddddddddddd"})
                except ValueError as exc:
                    assert "refused enrollment" in str(exc)
                else:
                    raise AssertionError("An unrelated enrollment failure was retried")
                failed.assert_called_once()


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
    result = {"id": "direct-q-1", "sequence": 7, "state": "acknowledged", "basis": "participant"}
    with mock.patch.object(mcp.dbond, "_read_pairing", return_value=pairing), \
         mock.patch.object(mcp.dbond, "direct_delivery_summary", return_value=result) as status, \
         mock.patch.object(mcp, "direct_send") as sender:
        assert mcp.call_tool("direct_status", {"alias": "fixture", "message_id": "direct-q-1", "sequence": 7}) == result
        status.assert_called_once_with(pairing, "direct-q-1", 7, wait_ms=0)
        sender.assert_not_called()
    owner_note = {"id": "owner-setup-1", "sequence": 3, "from": "owner",
                  "to": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "text": "Setup note"}
    sender_id = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    assert mcp.valid_direct(owner_note, pairing)["sender_id"] == "owner"
    wake_note = {**owner_note, "id": "@wake:11111111-2222-3333-4444-555555555555"}
    assert mcp.valid_direct(wake_note, pairing)["id"] == wake_note["id"]
    try:
        mcp.valid_direct({**wake_note, "from": sender_id}, pairing)
    except ValueError:
        pass
    else:
        raise AssertionError("A paired service spoofed an owner wake source")
    with mock.patch.object(mcp, "cli") as sender:
        try:
            mcp.direct_send("fixture", "owner", "Unsafe answer", source_id="owner-setup-1")
        except ValueError as exc:
            assert "paired service" in str(exc)
        else:
            raise AssertionError("owner note incorrectly accepted as an answer target")
        sender.assert_not_called()
    receipt = {"state": "verified", "sequence": 19, "duplicate": False, "id": "@wake:fixture"}
    with mock.patch.object(mcp, "cli", return_value=receipt) as ack:
        assert mcp.call_tool("acknowledge_wake", {"alias": "fixture", "sequence": 19}) == receipt
        ack.assert_called_once_with("wake-ack", "fixture", 19)
    with mock.patch.object(mcp.dbond, "session_status", return_value={"state":"subscriber_present"}) as status:
        assert mcp.call_tool("session_status", {"alias":"fixture"})["state"] == "subscriber_present"
        assert mcp.call_tool("session_status", {"alias":"fixture", "finish_registration":True})["state"] == "subscriber_present"
        assert status.call_args_list[0].kwargs == {"finish_registration":False}
        assert status.call_args_list[1].kwargs == {"finish_registration":True}


if __name__ == "__main__":
    exchange()
    exchange(True)
    exchange(interpreter="/usr/bin/python3")
    exchange(True, interpreter="/usr/bin/python3")
    exchange(native=True)
    enrollment_reuse()
    concurrent_code_sessions_same_title()
    direct_tool_commands()
    print("PASS: packed MCP entry points, Dot/direct channel pushes, manual direct wait, correlated tool commands, and pairing reuse")
