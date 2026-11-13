"""Isolated direct agent roundtrip through the real Hub HTTP and SQLite ledger."""
import json
import pathlib
import selectors
import socket
import sqlite3
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = pathlib.Path(subprocess.check_output(["swift", "build", "--show-bin-path"], cwd=ROOT, text=True).strip()) / "dbond-probe"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def line(proc):
    with selectors.DefaultSelector() as ready:
        ready.register(proc.stdout, selectors.EVENT_READ)
        assert ready.select(10), "probe did not respond"
    return json.loads(proc.stdout.readline())


def command(proc, value):
    proc.stdin.write(value + "\n")
    proc.stdin.flush()
    return line(proc)


def call(pair, suffix="", body=None, accept=None):
    endpoint = pair["endpoint"].removesuffix("/events") + "/messages" + suffix
    request = urllib.request.Request(endpoint, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization":"Bearer " + pair["secret"], "Content-Type":"application/json",
                 **({"Accept":accept} if accept else {})})
    try:
        with OPENER.open(request, timeout=16) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def event_call(pair, body):
    request = urllib.request.Request(pair["endpoint"], data=json.dumps(body).encode(),
        headers={"Authorization":"Bearer " + pair["secret"],"Content-Type":"application/json"})
    try:
        with OPENER.open(request, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def start(store):
    proc = subprocess.Popen([str(BIN), str(store), "0"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, bufsize=1)
    base = line(proc)["endpoint"]
    return proc, base


def stop(proc):
    command(proc, "list")
    proc.stdin.write("quit\n")
    proc.stdin.flush()
    assert proc.wait(10) == 0


def owner_request(store, value):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(store / "pair.sock"))
    sock.sendall((json.dumps(value) + "\n").encode())
    result = json.loads(sock.makefile("rb").readline())
    sock.close()
    return result


def run():
    with tempfile.TemporaryDirectory() as folder:
        store = pathlib.Path(folder)
        proc, base = start(store)
        claude = command(proc, 'json {"op":"pair","name":"Claude / Test","destination":"dot"}')
        codex = command(proc, 'json {"op":"pair","name":"Codex / Test","destination":"dot"}')
        standalone = owner_request(store, {"op":"pair","name":"Standalone / Test","destination":"peer"})
        assert standalone["destination"] == "peer"
        unrelated = command(proc, 'json {"op":"pair","name":"Local demo","destination":"demo"}')
        direct_peers = call(claude, "/peers")
        assert direct_peers[0] == 200 and {row["id"] for row in direct_peers[1]["services"]} == {codex["id"], standalone["id"]}
        assert call(standalone, body={"id":"standalone-1","to":codex["id"],"text":"Direct-only pairing works"})[0] == 202
        assert call(unrelated, "/peers")[0] == 403
        message = {"id":"hello-1","to":codex["id"],"text":"Claude asks Codex directly"}
        first = call(claude, body=message)
        assert first[0] == 202 and first[1]["duplicate"] is False
        assert call(claude, body=message)[1]["duplicate"] is True
        assert call(claude, body={**message,"text":"changed"})[0] == 409
        assert event_call(claude, {"id":"hello-1","payload":{"text":message["text"],"in_reply_to":None}})[0] == 409
        status, inbox = call(codex,"?after=0")
        assert status == 200 and len(inbox["messages"]) == 2
        row = next(row for row in inbox["messages"] if row["id"] == "hello-1")
        assert row["id"] == "hello-1" and row["from"] == claude["id"] and row["text"] == message["text"]
        assert call(unrelated, body=message)[0] == 403
        assert call(codex, body={"id":"bad-reply","to":unrelated["id"],"text":"wrong","in_reply_to":"hello-1"})[0] == 404
        answer = {"id":"answer-1","to":claude["id"],"text":"Codex answers Claude", "in_reply_to":"hello-1"}
        assert call(codex, body=answer)[0] == 202
        response = call(claude,"?after=0")[1]["messages"][0]
        assert response["id"] == "answer-1" and response["in_reply_to"] == "hello-1"
        assert call(codex, body={**answer,"in_reply_to":"unknown"})[0] == 422
        refused = owner_request(store, {"op":"send_direct_note","to":codex["id"],"id":"early-note","text":"Do not deliver"})
        assert "error" in refused
        # The recipient holds one event-driven SSE connection; no poll loop.
        parsed = urllib.parse.urlparse(codex["endpoint"])
        sock = socket.create_connection((parsed.hostname, parsed.port), timeout=5)
        sock.settimeout(5)
        reader = sock.makefile("rb")
        path = parsed.path.removesuffix("/events") + "/messages/stream?after=" + str(response["sequence"])
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{parsed.port}\r\n"
                      f"Authorization: Bearer {codex['secret']}\r\nAccept: text/event-stream\r\n\r\n").encode())
        assert b"200" in reader.readline()
        while reader.readline() not in (b"\r\n", b"\n"):
            pass
        assert next(row for row in command(proc, "list")["services"] if row["id"] == codex["id"])["directSubscribers"] == 1
        assert call(claude, body={"id":"stream-1","to":codex["id"],"text":"SSE wake"})[0] == 202
        assert reader.readline().startswith(b"id: ")
        frame = reader.readline()
        assert frame.startswith(b"data: ") and json.loads(frame[6:])["text"] == "SSE wake"
        assert reader.readline() in (b"\n", b"\r\n")
        owner_result = owner_request(store, {"op":"send_direct_note","to":codex["id"],"id":"owner-setup-1","text":"Setup note"})
        assert owner_result["state"] == "sent_to_listener"
        assert reader.readline().startswith(b"id: ")
        frame = reader.readline()
        assert json.loads(frame[6:])["from"] == "owner"
        reader.close(); sock.close()
        command(proc, "pause " + codex["id"])
        assert call(codex,"?after=0")[0] == 423
        assert call(claude, body={"id":"paused-1","to":codex["id"],"text":"blocked"})[0] == 423
        command(proc, "resume " + codex["id"])
        stop(proc)
        proc, base = start(store)
        for pair in (claude, codex, standalone):
            pair["endpoint"] = base + "/v1/services/" + pair["id"] + "/events"
        assert "hello-1" in {row["id"] for row in call(codex,"?after=0")[1]["messages"]}
        assert call(claude,"?after=0")[1]["messages"][0]["id"] == "answer-1"
        assert call(claude, body=message)[1]["duplicate"] is True
        command(proc, "remove " + codex["id"])
        assert call(claude, body={"id":"revoked-1","to":codex["id"],"text":"blocked"})[0] == 404
        assert call(codex,"?after=0")[0] == 401
        with sqlite3.connect(store / "ledger.sqlite") as db:
            assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        stop(proc)
    print(json.dumps({"result":"pass","checks":["existing Dot pairings exchange direct messages", "optional direct-only enrollment", "stable dedupe and conflict", "correlated answer", "event-driven SSE wake", "targeted owner note requires active listener", "offline replay after restart", "pause and revoke enforcement", "no Dot dispatch"]}))


if __name__ == "__main__":
    run()
