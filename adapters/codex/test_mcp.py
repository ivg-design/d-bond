import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "plugins/d-bond-local-control/server"))
sys.path.insert(0, str(HERE.parent / "cli"))
import owner
spec = importlib.util.spec_from_file_location("dbond_codex_mcp", HERE / "plugins/d-bond-local-control/server/mcp.py")
mcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp)
ID = "11111111-2222-3333-4444-555555555555"


class OwnerSocketTest(unittest.TestCase):
    def test_same_user_round_trip_and_error(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            os.chmod(directory, 0o700)
            path = Path(directory) / "pair.sock"
            server = socket.socket(socket.AF_UNIX)
            server.bind(str(path))
            os.chmod(path, 0o600)
            server.listen(2)
            seen = []

            def reply():
                for _ in range(2):
                    with server.accept()[0] as client:
                        data = client.recv(4096)
                        seen.append(json.loads(data))
                        response = {"services": []} if len(seen) == 1 else {"error": "version conflict"}
                        client.sendall(json.dumps(response).encode() + b"\n")
                server.close()

            worker = threading.Thread(target=reply)
            worker.start()
            self.assertEqual(owner.request({"op": "list"}, path), {"services": []})
            with self.assertRaisesRegex(ValueError, "version conflict"):
                owner.request({"op": "set_sop"}, path)
            worker.join()
            self.assertEqual(seen, [{"op": "list"}, {"op": "set_sop"}])


class MCPTest(unittest.TestCase):
    def test_both_plugin_manifests_and_staged_owner_match_server(self):
        plugin = HERE / "plugins/d-bond-local-control"
        outer = json.loads((plugin / "plugin.json").read_text())
        active = json.loads((plugin / ".codex-plugin/plugin.json").read_text())
        self.assertEqual(outer["version"], mcp.VERSION)
        self.assertEqual(active["version"], mcp.VERSION)
        self.assertEqual((plugin / "server/owner.py").read_bytes(),
                         (HERE.parent / "cli/owner.py").read_bytes())
        self.assertEqual((plugin / "server/direct.py").read_bytes(),
                         (HERE.parent / "direct/server/mcp.py").read_bytes())
        for module in ("dbond.py", "wake_registration.py", "codex_registration.py"):
            self.assertEqual((plugin / "server" / module).read_bytes(),
                             (HERE.parent / "cli" / module).read_bytes())

    def test_one_plugin_exposes_shared_direct_and_owner_tools_without_duplicate_names(self):
        names = [tool["name"] for tool in mcp.TOOLS]
        self.assertEqual(len(names), len(set(names)))
        for name in ("list_connections", "set_sop", "enroll_agent", "session_status",
                     "acknowledge_wake", "send_direct_message", "receive_direct_messages",
                     "answer_direct_message", "ask_dot", "answer_dot_push"):
            self.assertIn(name, names)
        with mock.patch.object(mcp.direct, "invoke", return_value={"state": "broker_recorded"}) as call:
            self.assertEqual(mcp.call("answer_direct_message", {"alias": "fixture"}),
                             {"state": "broker_recorded"})
        call.assert_called_once_with("answer_direct_message", {"alias": "fixture"})

    def test_generic_pairing_failure_does_not_exit_combined_server(self):
        requests = '\n'.join(json.dumps(row) for row in (
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "session_status", "arguments": {"alias": "missing"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )) + '\n'
        output = io.StringIO()
        with mock.patch.object(mcp.direct, "invoke", side_effect=SystemExit(2)), \
             mock.patch.object(sys, "stdin", io.StringIO(requests)), \
             mock.patch.object(sys, "stdout", output):
            mcp.run()
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(rows[0]["result"]["isError"])
        self.assertEqual(rows[1]["id"], 2)
        self.assertEqual(len(rows[1]["result"]["tools"]), 18)
        self.assertIn("acknowledge_direct_message", {tool["name"] for tool in rows[1]["result"]["tools"]})

    def test_local_status_is_read_only_and_requires_no_pairing(self):
        with mock.patch.object(mcp.owner, "local_status", return_value={"installation": "installed", "service": "offline"}) as check:
            self.assertEqual(mcp.call("local_status", {}), {"installation": "installed", "service": "offline"})
            with self.assertRaisesRegex(ValueError, "no arguments"):
                mcp.call("local_status", {"id": ID})
        check.assert_called_once_with()

    def test_trace_output_is_local_and_omits_subseconds_without_mutating_input(self):
        raw = {"events": [{"stages": [{"at": "2026-11-12T16:40:11.068+00:00"},
                                        {"at": "2026-11-12T16:40:11+00:00"}]}]}
        formatted = mcp.format_trace(raw)
        self.assertNotIn(".068", formatted["events"][0]["stages"][0]["at"])
        self.assertIn("2026", formatted["events"][0]["stages"][1]["at"])
        self.assertIn(".068", raw["events"][0]["stages"][0]["at"])

    def test_connection_list_adds_local_verification_time_without_changing_wire_value(self):
        stamp = "2026-11-12T20:00:00.123Z"
        source = {"services": [{"id": ID, "lastVerifiedAt": stamp},
                               {"id": "other", "lastVerifiedAt": None}]}
        with mock.patch.object(mcp.owner, "request", return_value=source):
            listed = mcp.call("list_connections", {})
        self.assertEqual(listed["services"][0]["lastVerifiedAt"], stamp)
        self.assertIn("2026", listed["services"][0]["lastVerifiedLocal"])
        self.assertNotIn(".123", listed["services"][0]["lastVerifiedLocal"])
        self.assertNotIn("lastVerifiedLocal", listed["services"][1])
        self.assertNotIn("lastVerifiedLocal", source["services"][0])

    def test_routes_and_version_check(self):
        original = mcp.owner.request
        seen = []
        try:
            mcp.owner.request = lambda payload: seen.append(payload) or {"ok": True}
            self.assertEqual(mcp.call("list_connections", {}), {"ok": True})
            self.assertEqual(mcp.call("get_sop", {"id": ID}), {"ok": True})
            self.assertEqual(mcp.call("set_sop", {"id": ID, "expectedVersion": 0,
                           "sop": {"version": 1, "rules": [{"id": "r1", "text": "Only notify after receipt"}]}}), {"ok": True})
            self.assertEqual([item["op"] for item in seen], ["list", "get_sop", "set_sop"])
            with self.assertRaisesRegex(ValueError, "expectedVersion"):
                mcp.call("set_sop", {"id": ID, "expectedVersion": 0,
                          "sop": {"version": 2, "rules": []}})
        finally:
            mcp.owner.request = original


if __name__ == "__main__":
    unittest.main()
