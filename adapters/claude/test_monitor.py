"""Isolated --push-only stream fixture. No Claude session, D-Bond app, or Dot send."""
import json
import os
import pathlib
import selectors
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


CLI = pathlib.Path(__file__).resolve().parents[1] / "cli" / "dbond.py"
SERVICE = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def run():
    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            assert self.path == f"/v1/services/{SERVICE}/events/stream?after=0"
            assert self.headers.get("Authorization") == "Bearer c2VjcmV0"
            assert self.headers.get("Accept") == "text/event-stream"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for sequence, ident, state, reply in [
                (1, "outgoing", "dot_stored", "req_00000000000000000000000000000000"),
                (2, "@dot:9", "dot_push", "What is 2 + 2?"),
            ]:
                row = {"id": ident, "sequence": sequence, "state": state, "reply": reply}
                self.wfile.write(f"id: {sequence}\ndata: {json.dumps(row)}\n\n".encode())
                self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory() as folder:
        home = pathlib.Path(folder)
        store = home / "Library" / "Application Support" / "D-Bond CLI"
        store.mkdir(parents=True, mode=0o700)
        os.chmod(store, 0o700)
        endpoint = f"http://127.0.0.1:{server.server_port}/v1/services/{SERVICE}/events"
        pairing = store / "claude-test.json"
        pairing.write_text(json.dumps({"endpoint": endpoint, "secret": "c2VjcmV0", "cursor": 0, "pushes": []}))
        os.chmod(pairing, 0o600)
        environment = dict(os.environ, HOME=str(home))
        process = subprocess.Popen(["python3", str(CLI), "listen", "claude-test", "--push-only"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, env=environment)
        try:
            with selectors.DefaultSelector() as ready:
                ready.register(process.stdout, selectors.EVENT_READ)
                assert ready.select(5), "listener did not emit a push"
            row = json.loads(process.stdout.readline())
            assert row == {"id": "@dot:9", "sequence": 2, "state": "dot_push", "text": "What is 2 + 2?"}, row
            saved = json.loads(pairing.read_text())
            assert saved["cursor"] == 2 and saved["pushes"] == ["@dot:9"]
        finally:
            process.terminate()
            process.wait(timeout=5)
            server.shutdown()
            server.server_close()
    print("PASS: authenticated push-only SSE stream, stable source ID and persisted cursor")


if __name__ == "__main__":
    run()
