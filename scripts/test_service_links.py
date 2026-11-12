"""Isolated lifecycle smoke test for named local service links."""
import json
import pathlib
import selectors
import socket
import sqlite3
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


def read(proc):
    with selectors.DefaultSelector() as ready:
        ready.register(proc.stdout, selectors.EVENT_READ)
        assert ready.select(10), "probe timed out"
    line = proc.stdout.readline()
    assert line, "probe exited"
    return json.loads(line)


def command(proc, value):
    proc.stdin.write(value + "\n")
    proc.stdin.flush()
    return read(proc)


def call(url, key, body=None):
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        with OPENER.open(request, timeout=16) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def run():
    entered = threading.Event()
    release = threading.Event()
    deliveries = []

    class Fixture(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            assert self.path == "/fixture"
            item = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            deliveries.append(item["id"])
            if item["id"] == "first":
                entered.set()
                release.wait(10)
            payload = json.dumps({
                "signed": True, "opaque": True, "started": True, "replied": True,
                "request_id": item["id"], "reply": "fixture:" + item["id"],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    worker = threading.Thread(target=fixture.serve_forever, daemon=True)
    worker.start()
    with tempfile.TemporaryDirectory() as store:
        def start():
            process = subprocess.Popen([str(BIN), store, str(fixture.server_port)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
            return process, read(process)["endpoint"]

        def stop(process):
            if process.poll() is None:
                process.stdin.write("quit\n")
                process.stdin.flush()
                assert process.wait(10) == 0

        try:
            proc, base = start()
            link = command(proc, "Render watcher")
            endpoint, secret, ident = link["endpoint"], link["secret"], link["id"]
            assert endpoint == base + "/v1/services/" + ident + "/events"
            assert command(proc, "list")["services"][0]["name"] == "Render watcher"
            peer_url = endpoint.removesuffix("/events") + "/peers"
            target = command(proc, "fixture-herald")
            assert call(peer_url, secret) == (200, {"services":[{"id":target["id"],"name":"fixture-herald","paused":False}]})
            assert call(peer_url, "different-key")[0] == 401
            assert command(proc, "remove " + target["id"])["removed"]
            assert call(peer_url, secret) == (200, {"services":[]})
            first = {"id": "first", "payload": {"value": 1}}
            second = {"id": "second", "payload": {"value": 2}}
            assert call(endpoint, secret, first)[0] == 202
            assert entered.wait(5), "first dispatch did not start"
            assert call(endpoint, secret, second)[0] == 202
            assert command(proc, "pause " + ident)["paused"] is True
            assert call(peer_url, secret)[0] == 423
            assert call(endpoint, secret, {"id": "third", "payload": {}})[0] == 423
            assert call(endpoint, secret, second)[1]["duplicate"] is True
            release.set()
            time.sleep(.3)
            assert deliveries == ["first"], deliveries
            listed = command(proc, "list")["services"][0]
            assert listed["paused"] and listed["queued"] == 1
            stop(proc)
            proc, base = start()
            assert command(proc, "list")["services"][0]["paused"] is True
            time.sleep(.2)
            assert deliveries == ["first"], deliveries
            assert command(proc, "resume " + ident)["paused"] is False
            rows = call(base + "/v1/services/" + ident + "/events/stream?after=0", secret)[1]["messages"]
            deadline = time.monotonic() + 5
            while len(rows) < 2 and time.monotonic() < deadline:
                rows = call(base + "/v1/services/" + ident + "/events/stream?after=0", secret)[1]["messages"]
            assert [row["id"] for row in rows] == ["first", "second"], rows
            assert deliveries == ["first", "second"], deliveries
            with socket.create_connection(("127.0.0.1", int(base.rsplit(":", 1)[1])), timeout=3) as stream:
                stream.settimeout(3)
                path = "/v1/services/" + ident + "/events/stream?after=" + str(rows[-1]["sequence"])
                request = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{base.rsplit(':', 1)[1]}\r\nAuthorization: Bearer {secret}\r\nAccept: text/event-stream\r\n\r\n"
                stream.sendall(request.encode())
                assert b"text/event-stream" in stream.recv(4096)
                deadline = time.monotonic() + 2
                while command(proc, "list")["services"][0]["subscribers"] != 1 and time.monotonic() < deadline:
                    time.sleep(.02)
                assert command(proc, "list")["services"][0]["subscribers"] == 1
                command(proc, "pause " + ident)
                assert stream.recv(4096) == b"", "paused SSE stream stayed open"
            assert command(proc, "list")["services"][0]["subscribers"] == 0
            assert proc.poll() is None, f"probe exited after SSE pause: {proc.returncode}"
            assert call(base + "/v1/services/" + ident + "/events/stream?after=0", secret)[0] == 423
            command(proc, "resume " + ident)
            assert call(base + "/v1/services/" + ident + "/events/stream?after=0", secret)[0] == 200
            assert command(proc, "remove " + ident)["removed"]
            assert call(base + "/v1/services/" + ident + "/events", secret, second)[0] == 401
            assert command(proc, "list")["services"] == []
            with sqlite3.connect(pathlib.Path(store) / "ledger.sqlite") as database:
                assert secret not in "".join(str(row) for row in database.execute("SELECT body FROM services"))
            origin = "chrome-extension://" + "a" * 32
            browser_link = command(proc, "--chrome-origin " + origin + " Browser sender")
            browser_url = browser_link["endpoint"]
            def browser_request(method, requested_origin, url=browser_url):
                request = urllib.request.Request(url, method=method, headers={
                    "Origin": requested_origin,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "authorization,content-type",
                })
                try:
                    with OPENER.open(request, timeout=5) as response:
                        return response.status
                except urllib.error.HTTPError as error:
                    return error.code
            assert browser_request("OPTIONS", origin) == 200
            assert browser_request("OPTIONS", origin, browser_url + "/stream") == 200
            assert browser_request("OPTIONS", "chrome-extension://" + "b" * 32) == 403
            assert browser_request("OPTIONS", "https://example.com") == 400
            other = command(proc, "Other sender")
            assert browser_request("OPTIONS", origin, other["endpoint"]) == 403
            command(proc, "remove " + other["id"])
            command(proc, "remove " + browser_link["id"])
            assert command(proc, "list")["services"] == []
            stop(proc)
            proc, _ = start()
            assert command(proc, "list")["services"] == []
            stop(proc)
        finally:
            fixture.shutdown()
            fixture.server_close()
    print("PASS: named link list, pause during dispatch, restart, queued replay, delete, key invalidation and exact browser origin")


if __name__ == "__main__":
    run()
