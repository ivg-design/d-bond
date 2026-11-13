"""Disposable broker wake challenge: authentication, generation, replay, and restart."""
import base64
import json
import re
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


def ack(probe, pair, token, secret=None):
    url = probe.endpoint + "/v1/services/" + pair["id"] + "/messages/wake-ack"
    request = urllib.request.Request(url, json.dumps({"token":token}).encode(),
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


class WakeRoundtripTests(unittest.TestCase):
    def test_explicit_retry_supersedes_old_token_and_filters_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            probe = Probe(directory)
            stream = replay = None
            try:
                target = probe.command({"op":"pair","name":"Target","destination":"peer"})
                first = owner(directory,{"op":"begin_wake_test","id":target["id"]})
                stream = Stream(probe,target)
                first_seq, first_event = stream.event()
                first_token = re.search(r"wake-ack ALIAS ([0-9a-f]{64})",first_event["text"]).group(1)
                second = owner(directory,{"op":"begin_wake_test","id":target["id"],"retry":True})
                self.assertNotEqual(second["wakeID"],first["wakeID"])
                self.assertEqual(second["wakeState"],"pending")
                self.assertEqual(ack(probe,target,first_token)[0],422)
                old = owner(directory,{"op":"trace","id":target["id"],"eventID":first["wakeID"]})["event"]
                self.assertEqual(old["state"],"wake_superseded")
                self.assertEqual(old["stages"][-1]["state"],"wake_superseded")
                self.assertEqual(old["preview"],"Session wake check")
                self.assertNotIn(first_token,json.dumps(old))
                with sqlite3.connect(directory / "ledger.sqlite") as db:
                    stored = json.loads(db.execute("SELECT body FROM events WHERE id=?",(first["wakeID"],)).fetchone()[0])
                self.assertNotIn(first_token,base64.b64decode(stored["payload"]).decode())
                replay = Stream(probe,target,after=0)
                replay_seq, replay_event = replay.event()
                self.assertEqual(replay_event["id"],second["wakeID"])
                self.assertGreater(replay_seq,first_seq)
                request = urllib.request.Request(probe.endpoint + "/v1/services/" + target["id"] + "/messages?after=0",
                    headers={"Authorization":"Bearer " + target["secret"]})
                with urllib.request.urlopen(request,timeout=5) as response:
                    self.assertEqual([row["id"] for row in json.load(response)["messages"]],[second["wakeID"]])
                second_token = re.search(r"wake-ack ALIAS ([0-9a-f]{64})",replay_event["text"]).group(1)
                self.assertEqual(ack(probe,target,second_token)[0],200)
                rejected = owner(directory,{"op":"begin_wake_test","id":target["id"],"retry":True})
                self.assertIn("error",rejected)
                self.assertEqual(owner(directory,{"op":"begin_wake_test","id":target["id"]})["wakeID"],second["wakeID"])
            finally:
                if replay: replay.close()
                if stream: stream.close()
                probe.close()

    def test_restart_supersedes_unacknowledged_epoch_before_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            probe = Probe(directory)
            stream = None
            try:
                target = probe.command({"op":"pair","name":"Target","destination":"peer"})
                first = owner(directory,{"op":"begin_wake_test","id":target["id"]})
                stream = Stream(probe,target)
                _, initial = stream.event()
                old_token = re.search(r"wake-ack ALIAS ([0-9a-f]{64})",initial["text"]).group(1)
                stream.close(); stream = None
                probe.close()
                probe = Probe(directory)
                stream = Stream(probe,target,after=0)
                _, current = stream.event()
                self.assertNotEqual(current["id"],first["wakeID"])
                self.assertEqual(ack(probe,target,old_token)[0],422)
                old = owner(directory,{"op":"trace","id":target["id"],"eventID":first["wakeID"]})["event"]
                self.assertEqual(old["state"],"wake_superseded")
            finally:
                if stream: stream.close()
                probe.close()

    def test_finish_registration_arms_one_delayed_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            probe = Probe(directory)
            stream = None
            try:
                target = probe.command({"op":"pair","name":"Target","destination":"peer"})
                first = owner(directory,{"op":"begin_wake_test","id":target["id"]})
                offline = owner(directory,{"op":"finish_registration","id":target["id"]})
                self.assertEqual(offline["wakeState"],"offline")
                self.assertFalse(offline["scheduled"])
                stream = Stream(probe,target)
                _, initial = stream.event()
                self.assertEqual(initial["id"],first["wakeID"])
                request = {"op":"finish_registration","id":target["id"]}
                armed = owner(directory,request)
                self.assertEqual(armed,{"id":target["id"],"wakeID":first["wakeID"],
                                        "wakeState":"pending","scheduled":True})
                self.assertEqual(owner(directory,request),armed)
                stream.sock.settimeout(13)
                _, delayed = stream.event()
                self.assertNotEqual(delayed["id"],first["wakeID"])
                old_token = re.search(r"wake-ack ALIAS ([0-9a-f]{64})",initial["text"]).group(1)
                self.assertEqual(ack(probe,target,old_token)[0],422)
                self.assertEqual(owner(directory,request)["scheduled"],False)
                with sqlite3.connect(directory / "ledger.sqlite") as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM events WHERE id LIKE '@wake:%'").fetchone()[0],2)
                new_token = re.search(r"wake-ack ALIAS ([0-9a-f]{64})",delayed["text"]).group(1)
                self.assertEqual(ack(probe,target,new_token)[0],200)
                self.assertEqual(owner(directory,request)["wakeState"],"verified")
            finally:
                if stream: stream.close()
                probe.close()

    def test_delayed_retry_does_not_replace_changed_paused_or_revoked_challenge(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            probe = Probe(directory)
            streams = []
            try:
                def raw(command):
                    probe.process.stdin.write(command + "\n")
                    probe.process.stdin.flush()
                    return json.loads(probe.process.stdout.readline())
                pairs = {}
                for name in ("Changed","Paused","Revoked"):
                    pair = probe.command({"op":"pair","name":name,"destination":"peer"})
                    pairs[name] = pair
                    owner(directory,{"op":"begin_wake_test","id":pair["id"]})
                    stream = Stream(probe,pair)
                    stream.event()
                    streams.append(stream)
                    self.assertTrue(owner(directory,{"op":"finish_registration","id":pair["id"]})["scheduled"])
                changed = pairs["Changed"]
                manual = owner(directory,{"op":"begin_wake_test","id":changed["id"],"retry":True})
                self.assertEqual(manual["wakeState"],"pending")
                paused = pairs["Paused"]
                raw("pause " + paused["id"])
                revoked = pairs["Revoked"]
                raw("remove " + revoked["id"])
                time.sleep(9.4)
                with sqlite3.connect(directory / "ledger.sqlite") as db:
                    rows = db.execute("SELECT body FROM events WHERE id LIKE '@wake:%'").fetchall()
                by_target = {}
                for (body,) in rows:
                    event = json.loads(body)
                    by_target.setdefault(event["route"],[]).append(event)
                self.assertEqual(len(by_target["peer:" + changed["id"]]),2)
                self.assertEqual(len(by_target["peer:" + paused["id"]]),1)
                self.assertNotIn("peer:" + revoked["id"],by_target)
                self.assertFalse(owner(directory,{"op":"finish_registration","id":changed["id"]})["scheduled"])
            finally:
                for stream in streams: stream.close()
                probe.close()

    def test_owner_challenge_requires_current_authenticated_recipient_ack(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            probe = Probe(directory)
            target = probe.command({"op":"pair","name":"Target","destination":"peer"})
            wrong = probe.command({"op":"pair","name":"Wrong","destination":"peer"})
            stream = None
            try:
                def raw(command):
                    probe.process.stdin.write(command + "\n")
                    probe.process.stdin.flush()
                    return json.loads(probe.process.stdout.readline())
                first = owner(directory,{"op":"begin_wake_test","id":target["id"]})
                repeat = owner(directory,{"op":"begin_wake_test","id":target["id"]})
                self.assertEqual(first,repeat)
                self.assertEqual(first["wakeState"],"offline")
                self.assertEqual(owner(directory,{"op":"list"})["services"][0]["wakeState"],"offline")
                stream = Stream(probe,target)
                seq, event = stream.event()
                self.assertEqual(event["id"],first["wakeID"])
                self.assertEqual(event["from"],"owner")
                forged_url = probe.endpoint + "/v1/services/" + wrong["id"] + "/messages"
                forged = urllib.request.Request(forged_url,
                    json.dumps({"id":"@wake:forged","to":target["id"],"text":"fake challenge"}).encode(),
                    {"Authorization":"Bearer " + wrong["secret"],"Content-Type":"application/json"})
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    urllib.request.urlopen(forged,timeout=5)
                self.assertEqual(rejected.exception.code,422)
                rejected.exception.close()
                token = re.search(r"wake-ack ALIAS ([0-9a-f]{64})",event["text"]).group(1)
                self.assertEqual(owner(directory,{"op":"list"})["services"][0]["wakeState"],"pending")
                self.assertEqual(ack(probe,target,token,wrong["secret"])[0],401)
                self.assertEqual(ack(probe,target,"0"*64)[0],422)
                self.assertEqual(ack(probe,target,token), (200,{"id":first["wakeID"],"state":"verified","duplicate":False}))
                self.assertEqual(ack(probe,target,token)[1]["duplicate"],True)
                status = owner(directory,{"op":"list"})["services"][0]
                self.assertEqual(status["wakeState"],"verified")
                self.assertEqual(status["subscribers"],0)
                self.assertEqual(status["directSubscribers"],1)
                self.assertIsNotNone(status["lastVerifiedAt"])
                trace = owner(directory,{"op":"trace","id":target["id"],"eventID":first["wakeID"]})["event"]
                self.assertEqual([row["state"] for row in trace["stages"]],["wake_pending","wake_verified"])
                self.assertEqual(trace["stages"][-1]["basis"],"authenticated_connection_ack")
                self.assertEqual(trace["preview"],"Session wake check")
                self.assertNotIn(token,json.dumps(trace))
                self.assertNotIn(token,json.dumps(owner(directory,{"op":"list_events","id":target["id"]})))
                self.assertEqual(owner(directory,{"op":"begin_wake_test","id":target["id"]})["wakeID"],first["wakeID"])
                # Pause closes the active stream. A later connection must get a fresh challenge.
                raw("pause " + target["id"])
                self.assertEqual(ack(probe,target,token)[0],423)
                for _ in range(100):
                    if owner(directory,{"op":"list"})["services"][0]["wakeState"] == "offline":
                        break
                    time.sleep(.01)
                self.assertEqual(owner(directory,{"op":"list"})["services"][0]["wakeState"],"offline")
                stream.close(); stream = None
                raw("resume " + target["id"])
                stream = Stream(probe,target,after=seq)
                seq2, event2 = stream.event()
                second = owner(directory,{"op":"begin_wake_test","id":target["id"]})
                self.assertNotEqual(second["wakeID"],first["wakeID"])
                self.assertEqual(second["wakeState"],"pending")
                self.assertEqual(event2["id"],second["wakeID"])
                token2 = re.search(r"wake-ack ALIAS ([0-9a-f]{64})",event2["text"]).group(1)
                self.assertEqual(ack(probe,target,token)[0],422)
                self.assertEqual(ack(probe,target,token2)[0],200)
                # No ACK starts another challenge or self-ping.
                self.assertEqual(owner(directory,{"op":"list"})["services"][0]["wakeState"],"verified")
                with sqlite3.connect(directory / "ledger.sqlite") as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM events WHERE id LIKE '@wake:%'").fetchone()[0],2)
                stream.close(); stream = None
                probe.close()
                with sqlite3.connect(directory / "ledger.sqlite") as db:
                    legacy = {"service":wrong["id"],"id":"@wake:legacy-peer",
                              "payload":base64.b64encode(b'{"text":"ordinary peer message"}').decode(),
                              "state":"peer_delivered","route":"peer:" + target["id"]}
                    db.execute("INSERT INTO events(service,id,body) VALUES(?,?,?)",
                               (wrong["id"],legacy["id"],json.dumps(legacy)))
                probe = Probe(directory)
                historical = owner(directory,{"op":"list"})["services"][0]
                self.assertEqual(historical["wakeState"],"offline")
                self.assertIsNotNone(historical["lastVerifiedAt"])
                legacy_trace = owner(directory,{"op":"trace","id":target["id"],"eventID":"@wake:legacy-peer"})["event"]
                self.assertFalse(legacy_trace["wakeCheck"])
                self.assertEqual(legacy_trace["direction"],"inbound")
                stream = Stream(probe,target,after=seq2)
                _, event3 = stream.event()
                third = owner(directory,{"op":"begin_wake_test","id":target["id"]})
                self.assertNotEqual(third["wakeID"],second["wakeID"])
                self.assertEqual(third["wakeID"],event3["id"])
            finally:
                if stream:
                    stream.close()
                probe.close()


if __name__ == "__main__":
    unittest.main()
