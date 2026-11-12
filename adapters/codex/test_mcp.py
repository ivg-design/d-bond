import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest

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
