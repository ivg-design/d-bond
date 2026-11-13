"""Disposable broker crash and Dot transport recovery, with one logical send."""
import json
import pathlib
import selectors
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = pathlib.Path(subprocess.check_output(["swift", "build", "--show-bin-path"], cwd=ROOT, text=True).strip()) / "dbond-probe"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def read_line(proc):
    with selectors.DefaultSelector() as selected:
        selected.register(proc.stdout, selectors.EVENT_READ)
        assert selected.select(10), "probe did not report readiness"
    line = proc.stdout.readline()
    assert line, f"probe exited {proc.poll()}"
    return json.loads(line)


def command(proc, value):
    proc.stdin.write(value + "\n")
    proc.stdin.flush()
    return read_line(proc)


def request(url, key, body=None):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        with OPENER.open(req, timeout=16) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def await_state(base, ident, key, event_id, expected, timeout=12):
    end = time.monotonic() + timeout
    url = f"{base}/v1/services/{ident}/events/stream?after=0"
    while time.monotonic() < end:
        status, payload = request(url, key)
        assert status == 200, (status, payload)
        rows = [row for row in payload["messages"] if row["id"] == event_id]
        if any(row["state"] == expected for row in rows):
            return rows[-1]
        time.sleep(.08)
    raise AssertionError(f"{event_id} never reached {expected}: {rows}")


def run():
    first_stored = threading.Event()
    release_first = threading.Event()
    lock = threading.Lock()
    attempts = {}
    logical = set()

    class Fixture(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            assert self.path == "/fixture-dot"
            event = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            event_id = event["id"]
            with lock:
                attempts[event_id] = attempts.get(event_id, 0) + 1
                count = attempts[event_id]
                if event_id != "transient" or count > 2:
                    logical.add(event_id)
            if event_id == "crash" and count == 1:
                first_stored.set()
                assert release_first.wait(12), "first accepted send was never released"
            if event_id == "transient" and count <= 2:
                self.send_error(503)
                return
            state = "dot_expired" if event_id == "terminal" else "dot_stored"
            body = json.dumps({"state": state, "reply": f"fixture {event_id}"}).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # The broker was killed after the fixture accepted this send.

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    thread.start()
    proc = None
    try:
        with tempfile.TemporaryDirectory(prefix="dbond-crash-proof-") as store:
            def start():
                process = subprocess.Popen(
                    [str(BIN), store, str(fixture.server_port)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, bufsize=1,
                )
                return process, read_line(process)["endpoint"]

            proc, base = start()
            pair = command(proc, 'json {"op":"pair","name":"Crash fixture","destination":"dot"}')
            ident, key = pair["id"], pair["secret"]
            endpoint = pair["endpoint"]
            assert request(endpoint, key, {"id": "crash", "payload": {"fixtureDot": True}})[0] == 202
            assert first_stored.wait(5), "send did not reach Dot fixture"
            proc.kill()
            assert proc.wait(5) != 0
            proc.stdin.close()
            proc.stdout.close()
            proc.stderr.close()
            release_first.set()

            proc, base = start()
            assert command(proc, "list")["services"][0]["id"] == ident
            assert request(base + f"/v1/services/{ident}/events", key, {"id": "crash", "payload": {"fixtureDot": True}})[1]["duplicate"] is True
            first_row = await_state(base, ident, key, "crash", "dot_stored")
            assert first_row["reply"] == "fixture crash"
            assert first_row["recorded_at"] and first_row["recorded_at"] == await_state(base,ident,key,"crash","dot_stored")["recorded_at"]
            assert attempts["crash"] == 2, attempts
            assert logical == {"crash"}, logical

            assert request(base + f"/v1/services/{ident}/events", key, {"id": "transient", "payload": {"fixtureDot": True}})[0] == 202
            await_state(base, ident, key, "transient", "dot_stored", timeout=18)
            assert attempts["transient"] == 3, attempts
            assert request(base + f"/v1/services/{ident}/events", key, {"id": "terminal", "payload": {"fixtureDot": True}})[0] == 202
            await_state(base, ident, key, "terminal", "dot_expired")
            time.sleep(2.5)
            assert attempts["terminal"] == 1, attempts

            # The original bearer can immediately open a new SSE connection after restart.
            with socket.create_connection(("127.0.0.1", int(base.rsplit(":", 1)[1])), timeout=3) as stream:
                stream.settimeout(3)
                path = f"/v1/services/{ident}/events/stream?after={first_row['sequence']}"
                message = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{base.rsplit(':', 1)[1]}\r\nAuthorization: Bearer {key}\r\nAccept: text/event-stream\r\n\r\n"
                stream.sendall(message.encode())
                assert b"text/event-stream" in stream.recv(4096)
            assert logical == {"crash", "transient", "terminal"}, logical
    finally:
        release_first.set()
        if proc and proc.poll() is None:
            proc.kill()
            proc.wait(5)
        if proc:
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe and not pipe.closed:
                    pipe.close()
        fixture.shutdown()
        fixture.server_close()
    print("PASS: crash replay, same pairing and SSE, transient recovery, terminal single delivery")


if __name__ == "__main__":
    run()
