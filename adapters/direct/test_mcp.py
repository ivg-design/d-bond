"""Generic stdio tool surface and Dot/direct routing separation."""
import contextlib
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import unittest
from unittest.mock import patch

SERVER = pathlib.Path(__file__).parent / "server/mcp.py"
spec = importlib.util.spec_from_file_location("direct_mcp", SERVER)
mcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp)


class DirectMCPTests(unittest.TestCase):
    def test_stdio_advertises_both_dot_and_direct_tools(self):
        requests = "\n".join(json.dumps(row) for row in [
            {"jsonrpc":"2.0","id":1,"method":"initialize","params":{}},
            {"jsonrpc":"2.0","id":2,"method":"tools/list"},
        ]) + "\n"
        process = subprocess.run([sys.executable, str(SERVER)], input=requests, text=True,
                                 capture_output=True, timeout=5, check=True)
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        names = {item["name"] for item in responses[1]["result"]["tools"]}
        self.assertTrue({"enroll_agent","list_direct_peers","send_direct_message",
                         "receive_direct_messages","answer_direct_message","ask_dot",
                         "wait_for_dot_push","answer_dot_push"} <= names)
        self.assertIn("does not wake an idle", responses[0]["result"]["instructions"])

    def test_direct_send_and_dot_ask_use_different_routes(self):
        pairing = {"endpoint":"http://127.0.0.1:1234/v1/services/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/events",
                   "secret":"fixture"}
        with patch.object(mcp.dbond, "_read_pairing", return_value=pairing), \
             patch.object(mcp.dbond, "_direct_target", return_value="11111111-2222-3333-4444-555555555555"), \
             patch.object(mcp.dbond, "_request", return_value=(202,{"id":"fixed-id","sequence":7,"duplicate":False})) as request:
            sent = mcp.invoke("send_direct_message", {"alias":"test","to":"Codex","text":"Hi","message_id":"fixed-id"})
            self.assertEqual(sent["state"], "broker_recorded")
            self.assertTrue(request.call_args.args[2].endswith("/messages"))
        with patch.object(mcp.dbond, "command_ask", side_effect=lambda args: print(json.dumps({"state":"dot_replied","reply":"Hello"}))) as ask:
            result = mcp.invoke("ask_dot", {"alias":"test","text":"Hi","to":"Codex","message_id":"dot-id"})
            self.assertEqual(result["state"], "dot_replied")
            self.assertEqual(ask.call_args.args[0].event_id, "dot-id")


if __name__ == "__main__":
    unittest.main()
