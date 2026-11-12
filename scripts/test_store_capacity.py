"""Durability and legacy migration smoke; never uses production state."""
import base64
import hashlib
import json
import pathlib
import selectors
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = pathlib.Path(subprocess.check_output(["swift", "build", "--show-bin-path"], cwd=ROOT, text=True).strip()) / "dbond-probe"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def read(proc):
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        assert selector.select(15), "probe readiness timeout"
    return json.loads(proc.stdout.readline())

def start(store):
    proc = subprocess.Popen([str(BIN), str(store), "0"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    return proc, read(proc)["endpoint"]

def command(proc, value):
    proc.stdin.write(value + "\n"); proc.stdin.flush()
    return read(proc)

def stop(proc):
    proc.stdin.write("quit\n"); proc.stdin.flush()
    assert proc.wait(10) == 0

def post(url, secret, body):
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Authorization":"Bearer " + secret,"Content-Type":"application/json"})
    try:
        with OPENER.open(request, timeout=15) as result: return result.status, json.load(result)
    except urllib.error.HTTPError as error: return error.code, json.load(error)

def count(store):
    with sqlite3.connect(store / "ledger.sqlite") as db:
        return db.execute("SELECT count(*) FROM events").fetchone()[0], db.execute("SELECT count(*) FROM receipts").fetchone()[0]

def finished(store):
    with sqlite3.connect(store / "ledger.sqlite") as db:
        return sum(json.loads(row[0])["state"] == "local_reply" for row in db.execute("SELECT body FROM events"))

def capacity():
    with tempfile.TemporaryDirectory() as folder:
        store = pathlib.Path(folder)
        proc, _ = start(store)
        link = command(proc, "Volume test")
        for index in range(620):
            status, ack = post(link["endpoint"], link["secret"], {"id":f"event-{index}","payload":{"index":index}})
            assert status == 202, (index,status,ack)
        deadline = time.monotonic() + 40
        while finished(store) < 620 and time.monotonic() < deadline: time.sleep(.05)
        assert count(store)[0] == 620 and finished(store) == 620
        stop(proc)
        proc, base = start(store)
        url = base + "/v1/services/" + link["id"] + "/events"
        for index in (0, 511, 512, 619):
            body = {"id":f"event-{index}","payload":{"index":index}}
            assert post(url,link["secret"],body)[1]["duplicate"] is True
            assert post(url,link["secret"],{"id":f"event-{index}","payload":{"index":-1}})[0] == 409
        status, _ = post(url,link["secret"],{"id":"event-620","payload":{"index":620}})
        assert status == 202
        stop(proc)
        assert count(store)[0] == 621
        with sqlite3.connect(store / "ledger.sqlite") as db:
            db.row_factory = sqlite3.Row
            row = json.loads(db.execute("SELECT body FROM events WHERE id='event-0'").fetchone()[0])
            assert row["payload"] == "" and row["payloadDigest"], "completed payload was not compacted"

def migration():
    with tempfile.TemporaryDirectory() as folder:
        store = pathlib.Path(folder)
        service = "00000000-0000-0000-0000-000000000001"
        secret = "legacy-credential"
        payload = json.dumps({"index":7},separators=(",", ":"),sort_keys=True).encode()
        queued_payload = json.dumps({"index":8},separators=(",", ":"),sort_keys=True).encode()
        legacy = {"services":[{"id":service,"name":"Legacy link","digest":hashlib.sha256(secret.encode()).hexdigest(),"herald":False}],
                  "events":[{"service":service,"id":"old-7","payload":base64.b64encode(payload).decode(),"state":"local_reply","sequence":1,"reply":"legacy result"},
                            {"service":service,"id":"old-8","payload":base64.b64encode(queued_payload).decode(),"state":"queued"}],"seq":1}
        (store / "ledger.json").write_text(json.dumps(legacy))
        proc, base = start(store)
        assert command(proc,"list")["services"][0]["destination"] == "demo"
        url = base + "/v1/services/" + service + "/events"
        assert post(url,secret,{"id":"old-7","payload":{"index":7}})[1]["duplicate"] is True
        deadline = time.monotonic() + 5
        while finished(store) < 2 and time.monotonic() < deadline: time.sleep(.02)
        assert finished(store) == 2
        stop(proc)
        proc, base = start(store)
        assert post(base + "/v1/services/" + service + "/events",secret,{"id":"old-7","payload":{"index":7}})[1]["duplicate"] is True
        assert post(base + "/v1/services/" + service + "/events",secret,{"id":"old-8","payload":{"index":8}})[1]["duplicate"] is True
        command(proc,"remove " + service)
        stop(proc)
        proc, _ = start(store)
        assert command(proc,"list")["services"] == [], "legacy JSON reimported after deletion"
        stop(proc)

if __name__ == "__main__":
    capacity()
    migration()
    print("PASS: 621 events, restart/idempotency, compacted payloads, one-time legacy migration")
