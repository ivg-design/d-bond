"""Isolated durable recipient receipts and sender NSCondition wait through real Hub HTTP."""
import concurrent.futures
import json
import pathlib
import select
import socket
import urllib.parse
import sqlite3
import tempfile
import threading
import time
from test_direct_messages import call, command, start, stop


def status(pair, message, **options):
    query = {"id":message["id"], "sequence":message["sequence"], "condition":"acknowledged", "timeout_ms":0, **options}
    from urllib.parse import urlencode
    return call(pair, "/status?" + urlencode(query))


def open_receipts(pair, after, *, lane="receipts-stream"):
    parsed=urllib.parse.urlparse(pair["endpoint"])
    sock=socket.create_connection((parsed.hostname,parsed.port),timeout=3)
    reader=sock.makefile("rb")
    path=parsed.path.removesuffix("/events") + "/messages/" + lane + "?after=" + str(after)
    sock.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{parsed.port}\r\n"
                  f"Authorization: Bearer {pair['secret']}\r\nAccept: text/event-stream\r\n\r\n").encode())
    assert b"200" in reader.readline()
    while reader.readline() not in (b"\r\n",b"\n"):
        pass
    return sock, reader


def read_receipt(reader):
    cursor=reader.readline()
    assert cursor.startswith(b"id: ")
    assert reader.readline() == b"event: receipt\n"
    line=reader.readline()
    assert line.startswith(b"data: ")
    row=json.loads(line[6:])
    assert row["receipt_sequence"] == int(cursor[4:])
    assert set(row) == {"id","from","to","sequence","receipt_sequence","state","adapter","basis","recorded_at"}
    assert reader.readline() == b"\n"
    return row


def read_message(reader):
    cursor = reader.readline()
    assert cursor.startswith(b"id: ")
    row = json.loads(reader.readline().removeprefix(b"data: "))
    assert row["sequence"] == int(cursor[4:])
    assert reader.readline() == b"\n"
    return row


def run():
    with tempfile.TemporaryDirectory() as folder:
        store = pathlib.Path(folder)
        proc, base = start(store)
        sender = command(proc, 'json {"op":"pair","name":"Sender","destination":"peer"}')
        recipient = command(proc, 'json {"op":"pair","name":"Recipient","destination":"peer"}')
        other = command(proc, 'json {"op":"pair","name":"Other","destination":"peer"}')
        posted = call(sender, body={"id":"receipt-1", "to":recipient["id"], "text":"receipt fixture"})
        message = posted[1]
        assert posted[0] == 202
        assert status(sender, message)[1]["state"] == "pending", "broker storage is not recipient acceptance"
        # Merely consuming a stored inbound frame also must not invent host acceptance.
        inbox = call(recipient, "?after=0")[1]["messages"]
        assert inbox[0]["sequence"] == message["sequence"]
        assert inbox[0]["recipient_consumed"] is False
        assert status(sender, message)[1]["state"] == "pending"
        body = {"from":sender["id"], "id":message["id"], "sequence":message["sequence"],
                "state":"received", "adapter":"fixture_host", "basis":"host_acceptance"}
        assert call(other, "/receipt", body)[0] == 404
        assert call(recipient, "/receipt", {**body, "sequence":message["sequence"] + 1})[0] == 404
        assert call(recipient, "/receipt", {**body, "from":other["id"]})[0] == 404
        assert call(recipient, "/receipt", {**body, "sequence":True})[0] == 422
        assert call(recipient, "/receipt", {**body, "sequence":1.5})[0] == 422
        assert call(recipient, "/receipt", {**body, "basis":"participant"})[0] == 422
        assert status(other, message)[0] == 404
        assert status(recipient, message)[0] == 404
        assert status(sender, message, timeout_ms=-1)[0] == 422
        seen = call(recipient, "/receipt", body)
        assert seen[0] == 200 and seen[1]["duplicate"] is False
        assert status(sender, message, condition="received")[1]["satisfied"] is True
        assert status(sender, message)[1]["satisfied"] is False
        timed = status(sender, message, timeout_ms=30)[1]
        assert timed["state"] == "received" and timed["satisfied"] is False
        first_receipt = seen[1]["receipt"]
        assert first_receipt["recorded_at"].endswith("Z")
        replay = call(recipient, "/receipt", body)
        assert replay[1]["duplicate"] is True and replay[1]["receipt"] == first_receipt
        # A blocked sender request is awakened by durable receipt publication, not polling.
        begun = threading.Event()
        def wait():
            begun.set()
            return status(sender, message, timeout_ms=5000)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            waiting = pool.submit(wait)
            assert begun.wait(1)
            time.sleep(0.05)
            assert not waiting.done()
            acknowledged = call(recipient, "/receipt", {**body, "state":"acknowledged"})
            completed = waiting.result(timeout=2)
        assert acknowledged[0] == 200 and completed[1]["satisfied"] is True
        assert completed[1]["basis"] == "host_acceptance"
        assert completed[1]["state"] == "acknowledged"
        assert call(recipient, "?after=0")[1]["messages"][0]["recipient_consumed"] is False, "host ACK does not prove explicit handling"
        assert status(sender, message, condition="received")[1]["satisfied"] is True
        participant = call(recipient, "/receipt", {**body, "state":"acknowledged", "basis":"participant"})[1]
        assert participant["status"]["basis"] == "participant" and len(participant["status"]["receipts"]) == 3
        # Late weaker per-adapter evidence cannot erase a stronger participant acknowledgment.
        call(recipient, "/receipt", {**body, "adapter":"second_host"})
        assert status(sender, message)[1]["basis"] == "participant"
        trace = command(proc, 'json ' + json.dumps({"op":"trace", "id":sender["id"], "eventID":message["id"]}))["event"]
        assert trace["state"] == "peer_acknowledged" and any(row["basis"] == "participant_reported" for row in trace["stages"])
        assert trace["deliveryMeaning"]
        assert trace["brokerSequence"] == message["sequence"], "receipts never move inbound message cursor"
        # No receipt is ever delivered as an ordinary inbound message.
        assert call(recipient, "?after=0")[1]["messages"] == [{**inbox[0], "recipient_consumed": True}]
        # Authenticated correlated reply acknowledges connection handling, without claiming model observation.
        original = call(sender, body={"id":"reply-proof", "to":recipient["id"], "text":"reply please"})[1]
        answer = call(recipient, body={"id":"actual-answer", "to":sender["id"], "text":"answer", "in_reply_to":"reply-proof"})[1]
        reply_status = status(sender, original)[1]
        assert reply_status["basis"] == "connection_reply" and reply_status["satisfied"] is True
        consumed_rows = call(recipient, "?after=0")[1]["messages"]
        assert next(row for row in consumed_rows if row["id"] == original["id"])["recipient_consumed"] is True
        assert call(sender, "?after=0")[1]["messages"][0]["recipient_consumed"] is False, "reply consumption is not inherited by the reverse message"
        assert call(recipient, body={"id":"actual-answer", "to":sender["id"], "text":"answer", "in_reply_to":"reply-proof"})[1]["sequence"] == answer["sequence"]
        assert len(status(sender, original)[1]["receipts"]) == 1
        call(recipient, body={"id":"stronger-answer", "to":sender["id"], "text":"answered", "in_reply_to":message["id"]})
        assert status(sender, message)[1]["basis"] == "participant", "connection reply must preserve stronger evidence"
        # Owner sees host acceptance semantics in event log, without model-seen claim.
        host_only = call(sender, body={"id":"host-only", "to":recipient["id"], "text":"automatic acknowledgment"})[1]
        call(recipient, "/receipt", {**body, "id":host_only["id"], "sequence":host_only["sequence"], "state":"acknowledged"})
        host_trace = command(proc, 'json ' + json.dumps({"op":"trace", "id":sender["id"], "eventID":"host-only"}))["event"]
        assert host_trace["deliveryMeaning"] == "Acknowledged by fixture_host."
        consumed_rows = call(recipient, "?after=0")[1]["messages"]
        assert next(row for row in consumed_rows if row["id"] == host_only["id"])["recipient_consumed"] is False
        message_stream, message_reader = open_receipts(recipient, 0, lane="stream")
        streamed = [read_message(message_reader) for _ in consumed_rows]
        message_reader.close(); message_stream.close()
        assert streamed == consumed_rows, "direct SSE carries exact recipient handling evidence without hiding history"
        # A separate sender metadata SSE remains open after ordinary status times out.
        cursor=max(row["receipt_sequence"] for row in status(sender, host_only)[1]["receipts"])
        late=call(sender,body={"id":"late-stream", "to":recipient["id"], "text":"late host acceptance"})[1]
        assert status(sender,late,timeout_ms=10)[1]["state"] == "pending"
        inbound_head=call(sender,"/head")[1]["sequence"]
        stream, reader=open_receipts(sender,cursor)
        isolated, isolated_reader=open_receipts(other,0)
        late_body={**body,"id":late["id"],"sequence":late["sequence"]}
        assert call(recipient,"/receipt",{**late_body,"state":"unconfirmed"})[0] == 422
        assert call(recipient,"/receipt",{**late_body,"state":"acknowledged","basis":"host_result_unknown"})[0] == 422
        # Only an actual adapter write with an unknown host result may report this issue.
        with concurrent.futures.ThreadPoolExecutor() as pool:
            waiting=pool.submit(status,sender,late,timeout_ms=5000)
            time.sleep(0.05)
            uncertain=call(recipient,"/receipt",{**late_body,"state":"unconfirmed","basis":"host_result_unknown"})[1]["receipt"]
            unconfirmed=waiting.result(timeout=2)[1]
        assert unconfirmed["state"] == "unconfirmed" and unconfirmed["satisfied"] is False
        assert status(sender,late,condition="received")[1]["satisfied"] is False
        assert read_receipt(reader) == uncertain
        uncertain_trace=command(proc,'json ' + json.dumps({"op":"trace","id":sender["id"],"eventID":late["id"]}))["event"]
        assert uncertain_trace["deliveryMeaning"] == "Delivery unconfirmed."
        assert uncertain_trace["state"] == "peer_unconfirmed"
        received=call(recipient,"/receipt",late_body)[1]["receipt"]
        assert read_receipt(reader) == received
        assert status(sender,late)[1]["state"] == "received", "real acceptance supersedes uncertain write"
        assert not select.select([isolated],[],[],0.05)[0], "other sender must not see receipt metadata"
        assert call(recipient,"/receipt",late_body)[1]["duplicate"] is True
        assert not select.select([stream],[],[],0.05)[0], "duplicate receipt must not echo another frame"
        stronger=call(recipient,"/receipt",{**late_body,"state":"acknowledged","basis":"participant"})[1]["receipt"]
        assert read_receipt(reader) == stronger
        assert call(sender,"/head")[1]["sequence"] == inbound_head, "receipt feed must not create an inbound message"
        for reader_,sock_ in ((reader,stream),(isolated_reader,isolated)):
            reader_.close();sock_.close()
        replay_stream,replay_reader=open_receipts(sender,received["receipt_sequence"])
        assert read_receipt(replay_reader) == stronger
        replay_reader.close();replay_stream.close()
        preserved = status(sender, message)[1]
        stop(proc)
        proc, base = start(store)
        for pair in (sender, recipient, other):
            pair["endpoint"] = base + "/v1/services/" + pair["id"] + "/events"
        assert status(sender, message)[1] == preserved
        assert next(row for row in call(recipient, "?after=0")[1]["messages"] if row["id"] == message["id"])["recipient_consumed"] is True, "explicit handling survives restart"
        replay_stream,replay_reader=open_receipts(sender,received["receipt_sequence"])
        assert read_receipt(replay_reader) == stronger, "receipt SSE resumes after restart with original timestamp"
        replay_reader.close();replay_stream.close()
        assert call(recipient, "/receipt", body)[1]["receipt"] == first_receipt
        # Pausing/revoking a sender wakes its existing waiter and revalidates authorization.
        pending = call(sender, body={"id":"pending", "to":recipient["id"], "text":"offline"})[1]
        with concurrent.futures.ThreadPoolExecutor() as pool:
            waiting = pool.submit(status, sender, pending, timeout_ms=5000)
            time.sleep(0.05)
            command(proc, "pause " + sender["id"])
            assert waiting.result(timeout=2)[0] == 423
        command(proc, "resume " + sender["id"])
        with concurrent.futures.ThreadPoolExecutor() as pool:
            waiting = pool.submit(status, sender, pending, timeout_ms=5000)
            time.sleep(0.05)
            command(proc, "remove " + sender["id"])
            assert waiting.result(timeout=2)[0] == 401
        with sqlite3.connect(store / "ledger.sqlite") as db:
            assert db.execute("SELECT count(*) FROM direct_receipts").fetchone()[0] == 0
        stop(proc)
    print(json.dumps({"result":"pass", "checks":["offline pending", "authenticated exact tuple", "condition wait and timeout", "per-adapter durable immutable timestamps", "retry and restart idempotence", "participant and host evidence distinction", "correlated reply acknowledgment", "silent metadata and stable inbound cursor", "owner event log meaning", "pause and revoke waiter wakeup", "late sender receipt SSE and credential isolation", "receipt cursor reconnect and restart replay without duplicate echo", "attempted unknown write is not ACK and positive receipt supersedes issue"]}))


if __name__ == "__main__":
    run()
