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
        self.assertTrue({"local_status","session_status","enroll_agent","list_direct_peers","send_direct_message",
                         "receive_direct_messages","answer_direct_message","ask_dot",
                         "wait_for_dot_push","answer_dot_push"} <= names)
        self.assertIn("does not wake an idle", responses[0]["result"]["instructions"])

    def test_local_status_requires_no_alias_or_pairing(self):
        with patch.object(mcp.owner, "local_status", return_value={"installation":"installed","service":"reachable"}) as check:
            self.assertEqual(mcp.invoke("local_status", {}), {"installation":"installed","service":"reachable"})
            with self.assertRaisesRegex(ValueError, "no arguments"):
                mcp.invoke("local_status", {"alias":"x"})
        check.assert_called_once_with()

    def test_acknowledge_wake_accepts_only_a_sequence(self):
        tool = next(t for t in mcp.TOOLS if t["name"] == "acknowledge_wake")
        self.assertEqual(tool["inputSchema"]["required"], ["alias", "sequence"])
        self.assertEqual(tool["inputSchema"]["properties"]["sequence"]["type"], "integer")
        with patch.object(mcp.wake_registration, "acknowledge", return_value={"sequence": 17}) as ack:
            self.assertEqual(mcp.invoke("acknowledge_wake", {"alias": "test", "sequence": 17}), {"sequence": 17})
            ack.assert_called_once_with("test", 17)

    def test_session_status_finish_is_explicit(self):
        with patch.object(mcp.dbond, "session_status", return_value={"alias":"test", "state":"pending_listener"}) as check:
            self.assertEqual(mcp.invoke("session_status", {"alias":"test"})["state"], "pending_listener")
            self.assertEqual(mcp.invoke("session_status", {"alias":"test", "finish_registration":True})["state"], "pending_listener")
        self.assertEqual(check.call_args_list[0].kwargs, {"finish_registration":False})
        self.assertEqual(check.call_args_list[1].kwargs, {"finish_registration":True})
        schema = next(tool for tool in mcp.TOOLS if tool["name"] == "session_status")
        self.assertEqual(schema["inputSchema"]["properties"]["finish_registration"], {"type":"boolean"})

    def test_enrollment_repairs_existing_codex_session_and_requests_wake(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        thread = "00000000-1111-2222-3333-444444444444"
        conn = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        alias = f"codex-{thread}"
        pair = {"endpoint": f"http://127.0.0.1:9000/v1/services/{conn}/events"}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "pair.json"
            path.touch()
            with patch.dict(mcp.codex_registration.os.environ, {"CODEX_THREAD_ID":thread}), \
                 patch.object(mcp.dbond, "_alias_path", return_value=path), \
                 patch.object(mcp.dbond, "_safe_dir", return_value=Path(directory)), \
                 patch.object(mcp.dbond, "_read_pairing", return_value=pair), \
                 patch.object(mcp.owner, "request", return_value={"services":[{"name":"Codex / First name","id":conn}]}), \
                 patch.object(mcp.dbond, "bind_session", return_value=pair) as bind, \
                 patch.object(mcp.dbond, "session_status", return_value={"wakeState":"offline"}) as status, \
                 patch.object(mcp.codex_registration, "attach", return_value={"state":"listening"}) as attach:
                result = mcp.invoke("enroll_agent", {"name":"Codex / Renamed", "thread_id":thread})
                self.assertTrue(result["reused"])
                self.assertEqual(result["wake"]["state"], "offline")
                bind.assert_called_once_with(alias, "codex", thread)
                status.assert_called_once_with(alias, finish_registration=False)
                attach.assert_called_once_with(alias, thread, pair)

    def test_enrollment_reuses_unique_custom_alias_for_exact_chat(self):
        from tempfile import TemporaryDirectory
        thread = "00000000-1111-2222-3333-444444444444"
        conn = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        pair = {"endpoint": f"http://127.0.0.1:9000/v1/services/{conn}/events",
                "client": "codex", "session_id": thread}
        with TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "my-old-alias.json"
            path.touch()
            with patch.dict(mcp.codex_registration.os.environ, {"CODEX_THREAD_ID": thread}), \
                 patch.object(mcp.dbond, "_safe_dir", return_value=path.parent), \
                 patch.object(mcp.dbond, "_alias_path", return_value=path), \
                 patch.object(mcp.dbond, "_read_pairing", return_value=pair), \
                 patch.object(mcp.owner, "request", return_value={"services": [{"id": conn, "name": "Codex / Earlier title"}]}), \
                 patch.object(mcp.dbond, "command_enroll") as enroll, \
                 patch.object(mcp.dbond, "bind_session"), \
                 patch.object(mcp.codex_registration, "attach", return_value={"state": "listening", "ready": True}), \
                 patch.object(mcp.dbond, "session_status", return_value={"registration_finish": {"scheduled": True}}) as finish:
                result = mcp.invoke("enroll_agent", {"name": "Codex / New title", "thread_id": thread})
            self.assertEqual(result["linked"], "my-old-alias")
            self.assertTrue(result["reused"])
            self.assertEqual(result["registration_finish"], {"scheduled": True})
            finish.assert_called_once_with("my-old-alias", finish_registration=True)
            enroll.assert_not_called()

    def test_enrollment_rejects_ambiguous_and_conflicting_bindings(self):
        from tempfile import TemporaryDirectory
        thread = "00000000-1111-2222-3333-444444444444"
        other = "99999999-1111-2222-3333-444444444444"
        with TemporaryDirectory() as directory:
            store = pathlib.Path(directory)
            for name in ("first", "second"):
                (store / (name + ".json")).touch()
            with patch.dict(mcp.codex_registration.os.environ, {"CODEX_THREAD_ID": thread}), \
                 patch.object(mcp.dbond, "_safe_dir", return_value=store), \
                 patch.object(mcp.dbond, "_read_pairing", return_value={"client": "codex", "session_id": thread}):
                with self.assertRaisesRegex(ValueError, "Multiple pairings"):
                    mcp.invoke("enroll_agent", {"name": "Codex / Same", "thread_id": thread})
            (store / "second.json").unlink()
            with patch.dict(mcp.codex_registration.os.environ, {"CODEX_THREAD_ID": thread}), \
                 patch.object(mcp.dbond, "_safe_dir", return_value=store), \
                 patch.object(mcp.dbond, "_read_pairing", return_value={"client": "codex", "session_id": thread}):
                with self.assertRaisesRegex(ValueError, "different alias"):
                    mcp.invoke("enroll_agent", {"name": "Codex / Same", "thread_id": thread, "alias": f"codex-{thread}"})
            (store / "first.json").unlink()
            conventional = store / f"codex-{thread}.json"
            conventional.touch()
            with patch.dict(mcp.codex_registration.os.environ, {"CODEX_THREAD_ID": thread}), \
                 patch.object(mcp.dbond, "_safe_dir", return_value=store), \
                 patch.object(mcp.dbond, "_alias_path", return_value=conventional), \
                 patch.object(mcp.dbond, "_read_pairing", return_value={"client": "codex", "session_id": other}), \
                 patch.object(mcp.owner, "request", return_value={"services": []}):
                with self.assertRaisesRegex(ValueError, "different host session"):
                    mcp.invoke("enroll_agent", {"name": "Codex / Same", "thread_id": thread})
            conventional.unlink()
            unbound = store / "unbound.json"
            unbound.touch()
            conn = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            with patch.dict(mcp.codex_registration.os.environ, {"CODEX_THREAD_ID": thread}), \
                 patch.object(mcp.dbond, "_safe_dir", return_value=store), \
                 patch.object(mcp.dbond, "_alias_path", return_value=unbound), \
                 patch.object(mcp.dbond, "_read_pairing", return_value={"endpoint": f"http://127.0.0.1:9000/v1/services/{conn}/events"}), \
                 patch.object(mcp.owner, "request", return_value={"services": [{"id": conn, "name": "Codex / Same"}]}):
                with self.assertRaisesRegex(ValueError, "different service or topic"):
                    mcp.invoke("enroll_agent", {"name": "Codex / Same", "thread_id": thread, "alias": "unbound"})

    def test_same_title_gets_suffix_only_on_new_connection_collision(self):
        thread = "00000000-1111-2222-3333-444444444444"
        rows = [{"name": "Codex / Same", "id": "other"}]
        self.assertEqual(mcp._available_codex_title("Codex / Unique", thread, rows), "Codex / Unique")
        self.assertEqual(mcp._available_codex_title("Codex / Same", thread, rows), "Codex / Same · 00000000")
        rows.append({"name": "codex / same · 00000000", "id": "another"})
        self.assertEqual(mcp._available_codex_title("Codex / Same", thread, rows), "Codex / Same · 00000000-111")

    def test_new_same_title_chat_enrolls_distinct_alias_with_suffixed_name(self):
        from tempfile import TemporaryDirectory
        thread = "00000000-1111-2222-3333-444444444444"
        conn = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        pair = {"endpoint": f"http://127.0.0.1:9000/v1/services/{conn}/events"}
        with TemporaryDirectory() as directory:
            store = pathlib.Path(directory)
            captured = []
            def enroll(args):
                captured.append(args)
                print(json.dumps({"linked": args.alias, "reused": False}))
            with patch.dict(mcp.codex_registration.os.environ, {"CODEX_THREAD_ID": thread}), \
                 patch.object(mcp.dbond, "_safe_dir", return_value=store), \
                 patch.object(mcp.dbond, "_alias_path", return_value=store / f"codex-{thread}.json"), \
                 patch.object(mcp.dbond, "_read_pairing", return_value=pair), \
                 patch.object(mcp.owner, "request", return_value={"services": [{"id": "existing", "name": "Codex / Same"}]}), \
                 patch.object(mcp.dbond, "command_enroll", side_effect=enroll), \
                 patch.object(mcp.dbond, "bind_session"), \
                 patch.object(mcp.codex_registration, "attach", return_value={"state": "degraded", "ready": False}):
                result = mcp.invoke("enroll_agent", {"name": "Codex / Same", "thread_id": thread})
            self.assertEqual(result["linked"], f"codex-{thread}")
            self.assertEqual(captured[0].name, "Codex / Same · 00000000")
            self.assertEqual(captured[0].alias, f"codex-{thread}")

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
