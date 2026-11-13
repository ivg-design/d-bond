"""Disposable broker wake acknowledgments, enrollment, replay and restart."""
import base64
import json
import socket
import sqlite3
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from test_event_log_pages import Probe


def owner(directory, request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(directory / "pair.sock"))
        client.sendall((json.dumps(request) + "\n").encode())
        return json.loads(client.makefile("rb").readline())


def ack(probe, pair, sequence, secret=None):
    url = probe.endpoint + "/v1/services/" + pair["id"] + "/messages/wake-ack"
    request = urllib.request.Request(url, json.dumps({"sequence":sequence}).encode(),
        {"Authorization":"Bearer " + (secret or pair["secret"]),"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.load(error)
        finally:
            error.close()


class Stream:
    def __init__(self, probe, pair, after=0):
        from urllib.parse import urlparse
        endpoint = urlparse(probe.endpoint)
        self.sock = socket.create_connection((endpoint.hostname, endpoint.port), timeout=5)
        self.sock.settimeout(5)
        self.reader = self.sock.makefile("rb")
        path = f"/v1/services/{pair['id']}/messages/stream?after={after}"
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{endpoint.port}\r\n"
                           f"Authorization: Bearer {pair['secret']}\r\nAccept: text/event-stream\r\n\r\n").encode())
        assert b"200" in self.reader.readline()
        while self.reader.readline() not in (b"\r\n", b"\n"):
            pass

    def event(self):
        header = self.reader.readline()
        assert header.startswith(b"id: "), header
        data = self.reader.readline()
        assert data.startswith(b"data: "), data
        assert self.reader.readline() in (b"\n", b"\r\n")
        return int(header[4:]), json.loads(data[6:])

    def close(self):
        self.reader.close()
        self.sock.close()


def stored_events(directory, pair):
    with sqlite3.connect(directory / "ledger.sqlite") as db:
        rows = db.execute("SELECT body FROM events WHERE service='owner' ORDER BY rowid").fetchall()
    return [event for (raw,) in rows if (event := json.loads(raw)).get("route") == "peer:" + pair["id"]]


def register(directory, pair):
    return owner(directory, {"op":"finish_registration", "id":pair["id"]})


def manual(directory, pair, retry=False):
    return owner(directory, {"op":"begin_wake_test", "id":pair["id"], "retry":retry})


def raw(probe, command):
    probe.process.stdin.write(command + "\n")
    probe.process.stdin.flush()
    return json.loads(probe.process.stdout.readline())


class WakeRoundtripTests(unittest.TestCase):
    def test_ack_uses_delivery_sequence_and_pairing(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            probe = Probe(directory)
            stream = replay = None
            try:
                target = probe.command({"op":"pair","name":"Target","destination":"peer"})
                wrong = probe.command({"op":"pair","name":"Wrong","destination":"peer"})
                first = manual(directory, target)
                self.assertEqual(manual(directory, target), first)
                self.assertEqual(first["wakeState"], "offline")
                stream = Stream(probe,target)
                seq, message = stream.event()
                self.assertEqual(message["id"], first["wakeID"])
                self.assertEqual(message["from"], "owner")
                self.assertIn(f"wake-ack ALIAS {seq}.", message["text"])
                self.assertNotIn("token", json.dumps(message))
                stored = stored_events(directory,target)[0]
                self.assertNotIn("wake_token",base64.b64decode(stored["payload"]).decode())
                forged_url = probe.endpoint + "/v1/services/" + wrong["id"] + "/messages"
                forged = urllib.request.Request(forged_url,
                    json.dumps({"id":"@wake:forged","to":target["id"],"text":"forged check"}).encode(),
                    {"Authorization":"Bearer " + wrong["secret"],"Content-Type":"application/json"})
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    urllib.request.urlopen(forged, timeout=5)
                self.assertEqual(rejected.exception.code,422)
                rejected.exception.close()
                self.assertEqual(ack(probe,target,seq,wrong["secret"])[0],401)
                self.assertEqual(ack(probe,wrong,seq)[0],422)
                for bad in (True, "1", 0, -1, 1.25, seq + 1000):
                    self.assertEqual(ack(probe,target,bad)[0],422)
                raw(probe,"pause " + target["id"])
                self.assertEqual(ack(probe,target,seq)[0],423)
                raw(probe,"resume " + target["id"])
                self.assertEqual(ack(probe,target,seq), (200, {"sequence":seq,"id":message["id"],"state":"verified","duplicate":False}))
                self.assertTrue(ack(probe,target,seq)[1]["duplicate"])
                trace = owner(directory,{"op":"trace","id":target["id"],"eventID":first["wakeID"]})["event"]
                self.assertEqual([stage["state"] for stage in trace["stages"]], ["wake_pending","wake_verified"])
                self.assertEqual(trace["stages"][-1]["basis"],"authenticated_connection_ack")
                self.assertEqual(trace["preview"],"Session wake check")
                # A fresh explicit test supersedes the previous receipt for ACK purposes.
                second = manual(directory,target,True)
                replay = Stream(probe,target,after=0)
                seq2, message2 = replay.event()
                self.assertEqual(message2["id"], second["wakeID"])
                self.assertGreater(seq2,seq)
                self.assertEqual(ack(probe,target,seq)[0],422)
                self.assertEqual(ack(probe,target,seq2)[0],200)
            finally:
                if replay: replay.close()
                if stream: stream.close()
                probe.close()

    def test_retry_supersedes_pending_and_hides_it_from_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            probe = Probe(directory)
            stream = replay = None
            try:
                target = probe.command({"op":"pair","name":"Target","destination":"peer"})
                first = manual(directory,target)
                stream = Stream(probe,target)
                seq, message = stream.event()
                self.assertEqual(message["id"],first["wakeID"])
                second = manual(directory,target,True)
                self.assertNotEqual(first["wakeID"],second["wakeID"])
                self.assertEqual(ack(probe,target,seq)[0],422)
                replay = Stream(probe,target,after=0)
                seq2,message2 = replay.event()
                self.assertEqual(message2["id"],second["wakeID"])
                self.assertEqual([e["state"] for e in stored_events(directory,target)], ["wake_superseded","wake_pending"])
                url = probe.endpoint + "/v1/services/" + target["id"] + "/messages?after=0"
                request = urllib.request.Request(url,headers={"Authorization":"Bearer " + target["secret"]})
                with urllib.request.urlopen(request,timeout=5) as response:
                    self.assertEqual([row["sequence"] for row in json.load(response)["messages"]], [seq2])
            finally:
                if replay: replay.close()
                if stream: stream.close()
                probe.close()

    def test_restart_migrates_legacy_pending_check_then_remembers_ack(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            probe = Probe(directory)
            stream = None
            try:
                target = probe.command({"op":"pair","name":"Target","destination":"peer"})
                other = probe.command({"op":"pair","name":"Other","destination":"peer"})
                first = manual(directory,target)
                stored = stored_events(directory,target)[0]
                seq = stored["sequence"]
                probe.close()
                probe = None
                with sqlite3.connect(directory/"ledger.sqlite") as db:
                    old = "a" * 64
                    payload = json.dumps({"text":"wake-ack ALIAS " + old,"wake_token":old})
                    stored["payload"] = base64.b64encode(payload.encode()).decode()
                    db.execute("UPDATE events SET body=? WHERE service='owner' AND id=?", (json.dumps(stored),first["wakeID"]))
                    # A similarly named peer message is never a broker check.
                    peer = {"service":other["id"],"id":"@wake:legacy-peer","state":"peer_delivered",
                            "payload":base64.b64encode(b'{"text":"ordinary peer message"}').decode(),
                            "route":"peer:" + target["id"]}
                    db.execute("INSERT INTO events(service,id,body) VALUES(?,?,?)",(other["id"],peer["id"],json.dumps(peer)))
                probe = Probe(directory)
                stream = Stream(probe,target)
                n, msg = stream.event()
                self.assertEqual((seq,first["wakeID"]),(n,msg["id"]))
                self.assertNotIn(old,json.dumps(msg))
                self.assertNotIn(old,base64.b64decode(stored_events(directory,target)[0]["payload"]).decode())
                self.assertFalse(owner(directory,{"op":"trace","id":target["id"],"eventID":peer["id"]})["event"]["wakeCheck"])
                self.assertFalse(register(directory,target)["scheduled"])
                self.assertEqual(ack(probe,target,seq)[0],200)
                stream.close();stream=None
                probe.close();probe=Probe(directory)
                status = owner(directory,{"op":"list"})["services"][0]
                self.assertEqual(status["wakeState"],"offline")
                self.assertIsNotNone(status["lastVerifiedAt"])
                stream = Stream(probe,target,after=seq)
                # HTTP headers can precede stream attachment.
                for _ in range(50):
                    result = register(directory,target)
                    if result["wakeState"] == "verified": break
                    time.sleep(.01)
                self.assertEqual(result,{"id":target["id"],"wakeID":first["wakeID"],"wakeState":"verified","scheduled":False})
                self.assertTrue(ack(probe,target,seq)[1]["duplicate"])
                self.assertEqual(len(stored_events(directory,target)),1)
            finally:
                if stream: stream.close()
                if probe: probe.close()

    def test_first_registration_delivers_once_after_setup_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            probe = Probe(directory)
            stream = None
            try:
                target = probe.command({"op":"pair","name":"Target","destination":"peer"})
                self.assertEqual(register(directory,target)["wakeState"],"offline")
                stream = Stream(probe,target)
                for _ in range(50):
                    first = register(directory,target)
                    if first["scheduled"]: break
                    time.sleep(.01)
                self.assertTrue(first["scheduled"])
                self.assertEqual(first, register(directory,target))
                self.assertEqual(stored_events(directory,target)[0]["state"],"wake_scheduled")
                stream.close();stream=None
                probe.close();probe=Probe(directory)
                stream = Stream(probe,target)
                stream.sock.settimeout(13)
                seq, event = stream.event()
                self.assertEqual(event["id"],first["wakeID"])
                self.assertFalse(register(directory,target)["scheduled"])
                self.assertFalse(ack(probe,target,seq)[1]["duplicate"])
                self.assertEqual(len(stored_events(directory,target)),1)
            finally:
                if stream: stream.close()
                probe.close()


if __name__ == "__main__":
    unittest.main()
