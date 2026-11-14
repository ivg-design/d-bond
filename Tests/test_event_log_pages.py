"""Isolated persisted-ledger pagination check; no production identity or service."""
import base64
import json
import socket
import sqlite3
import subprocess
import tempfile
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / ".build/release/dbond-probe"


class Probe:
    def __init__(self, directory):
        self.process = subprocess.Popen([str(PROBE), str(directory), "0"], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        self.endpoint = json.loads(self.process.stdout.readline())["endpoint"]

    def command(self, obj):
        self.process.stdin.write("json " + json.dumps(obj) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        assert line, self.process.stderr.read()
        return json.loads(line)

    def close(self):
        self.process.stdin.write("quit\n")
        self.process.stdin.flush()
        self.process.communicate(timeout=5)
        assert self.process.returncode == 0


class EventLogPageTests(unittest.TestCase):
    def test_direct_logs_show_both_sides_and_disambiguate_collisions(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            probe = Probe(directory)
            a = probe.command({"op":"pair", "name":"Agent A", "destination":"peer"})
            b = probe.command({"op":"pair", "name":"Agent B", "destination":"peer"})
            c = probe.command({"op":"pair", "name":"Agent C", "destination":"peer"})
            d = probe.command({"op":"pair", "name":"Dot fixture", "destination":"dot"})
            try:
                def send(sender, target, event_id, in_reply_to=None, text="Fixture"):
                    url = probe.endpoint + "/v1/services/" + sender["id"] + "/messages"
                    body = json.dumps({"id":event_id,"to":target["id"],"text":text,
                                       **({"in_reply_to":in_reply_to} if in_reply_to else {})}).encode()
                    request = urllib.request.Request(url, body,
                        {"Content-Type":"application/json","Authorization":"Bearer " + sender["secret"]})
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 202)
                        return json.load(response)["sequence"]

                def owner(request):
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                        client.connect(str(directory / "pair.sock"))
                        client.sendall((json.dumps(request) + "\n").encode())
                        return json.loads(client.makefile("rb").readline())

                send(a,b,"same-id")
                send(c,b,"same-id")
                send(b,a,"same-id")
                send(b,b,"self-id")
                b_events = owner({"op":"list_events","id":b["id"],"limit":10})["events"]
                self.assertEqual([(row["direction"],row["counterpartID"],row["eventID"])
                                  for row in b_events],
                                 [("self",b["id"],"self-id"),("outbound",a["id"],"same-id"),
                                  ("inbound",c["id"],"same-id"),("inbound",a["id"],"same-id")])
                self.assertEqual(b_events[-1]["counterpartName"],"Agent A")
                self.assertEqual(b_events[-1]["stages"][0]["basis"],"broker_recorded")
                # POST acceptance records the message; no recipient receipt exists yet.
                self.assertEqual(b_events[-1]["deliveryMeaning"], "Queued for the recipient.")
                self.assertEqual(owner({"op":"list_events","id":a["id"]})["events"][0]["direction"],"inbound")
                self.assertIn("ambiguous", owner({"op":"trace","id":b["id"],"eventID":"same-id"})["error"])
                selected = owner({"op":"trace","id":b["id"],"eventID":"same-id",
                                  "direction":"inbound","counterpartID":c["id"]})["event"]
                self.assertEqual(selected["service"],c["id"])
                self.assertEqual(selected["counterpartName"],"Agent C")
                self.assertEqual(owner({"op":"trace","id":b["id"],"eventID":"self-id"})["event"]["direction"],"self")
                first = probe.command({"op":"trace_page","id":b["id"],"limit":2})
                second = probe.command({"op":"trace_page","id":b["id"],"limit":2,
                                        "through":first["through"],"before":first["next"]})
                self.assertEqual(first["ids"] + second["ids"],
                                 ["self-id","same-id","same-id","same-id"])
                self.assertEqual(first["total"],4)
                self.assertIsNone(second["next"])
                pong_sequence = send(b,a,"pong-id",in_reply_to="same-id",text="PONG fixture-01")
                send(a,b,"code-id",text="```swift\nlet answer = 42\nprint(answer)\n```")
                send(a,b,"prose-id",text="A short summary. A second sentence.\n\nA long document follows with supporting material.")
                send(a,b,"wake-text-id",text="Keep this @wake:private-token out of the preview.")
                probe.close()
                with sqlite3.connect(directory / "ledger.sqlite") as db:
                    raw = db.execute("SELECT body FROM events WHERE service=? AND id=?",(b["id"],"pong-id")).fetchone()[0]
                    spent = json.loads(raw)
                    spent["payload"] = ""
                    db.execute("UPDATE events SET body=? WHERE service=? AND id=?",
                               (json.dumps(spent),b["id"],"pong-id"))
                    wake = {"service":"owner","id":"@wake:fixture","payload":base64.b64encode(
                        json.dumps({"text":"wake @wake:secret","wake_token":"sensitive"}).encode()).decode(),
                        "state":"wake_verified","route":"peer:"+a["id"],"stages":[]}
                    db.execute("INSERT INTO events(service,id,body) VALUES(?,?,?)",
                               ("owner","@wake:fixture",json.dumps(wake)))
                    legacy = {"service":a["id"],"id":"legacy-id","payload":base64.b64encode(
                        json.dumps({"text":"Legacy readable body. More details follow."}).encode()).decode(),
                        "state":"local_reply","stages":[]}
                    db.execute("INSERT INTO events(service,id,body) VALUES(?,?,?)",
                               (a["id"],"legacy-id",json.dumps(legacy)))
                    old_push = {"service":d["id"],"id":"@dot:1","payload":"","state":"dot_push","stages":[]}
                    db.execute("INSERT INTO events(service,id,body) VALUES(?,?,?)",
                               (d["id"],"@dot:1",json.dumps(old_push)))
                    receipt = {"service":d["id"],"id":"@dot:1","sequence":999,"state":"dot_push",
                               "reply":json.dumps({"message":"Dot says hello."})}
                    db.execute("INSERT INTO receipts(sequence,body) VALUES(?,?)",(999,json.dumps(receipt)))
                    for seq, event_id, answer in [
                        (1000,"dot-code","```swift\nlet answer = 42\nprint(answer)\n```"),
                        (1001,"dot-job",json.dumps({"title":"Example item saved","record":{"content":"opaque record text"}})),
                        (1002,"dot-unknown",json.dumps({"secret":"must not appear"})),
                        (1003,"dot-blocked",json.dumps({"title":"Request blocked","details":{"reason":"Additional information needed"}})),
                        (1004,"dot-potential",json.dumps({"text":"Needs review. Provide more context.","details":{"state":"pending"}})),
                        (1005,"dot-secret",json.dumps({"metadata":{"reason":"Bearer secret: must not appear"}})),
                        (1006,"dot-result",json.dumps({"title":"Example result returned","result":{"content":"opaque result text"}}))]:
                        event = {"service":d["id"],"id":event_id,"payload":"","state":"dot_replied",
                                 "preview":"Please review this request.","stages":[{"state":"dot_replied",
                                 "at":"2026-11-12T20:00:00Z","basis":"native_observed"}]}
                        db.execute("INSERT INTO events(service,id,body) VALUES(?,?,?)",
                                   (d["id"],event_id,json.dumps(event)))
                        final = {"service":d["id"],"id":event_id,"sequence":seq,"state":"dot_replied",
                                 "reply":answer,"recordedAt":"2026-11-12T20:00:01Z"}
                        db.execute("INSERT INTO receipts(sequence,body) VALUES(?,?)",(seq,json.dumps(final)))
                probe = Probe(directory)
                url = f"{probe.endpoint}/v1/services/{d['id']}/events/history?after=998&through=999"
                request = urllib.request.Request(url, headers={"Authorization":"Bearer " + d["secret"]})
                with urllib.request.urlopen(request, timeout=2) as response:
                    history = json.load(response)
                    self.assertEqual([row["id"] for row in history["messages"]], ["@dot:1"])
                    self.assertEqual(history["lastSequence"], 999)
                    self.assertFalse(history["hasMore"])
                pong = owner({"op":"trace","id":a["id"],"eventID":"pong-id"})["event"]
                self.assertEqual(pong["direction"],"inbound")
                self.assertEqual(pong["counterpartID"],b["id"])
                self.assertEqual(pong["inReplyTo"],"same-id")
                self.assertEqual(pong["brokerSequence"],pong_sequence)
                self.assertEqual(pong["stages"][0]["state"],"peer_delivered")
                self.assertEqual(pong["preview"],"PONG fixture-01")
                self.assertEqual(owner({"op":"trace","id":b["id"],"eventID":"pong-id"})["event"]["preview"],"PONG fixture-01")
                self.assertEqual(owner({"op":"trace","id":b["id"],"eventID":"code-id"})["event"]["preview"],"Code (swift): let answer = 42")
                self.assertEqual(owner({"op":"trace","id":b["id"],"eventID":"prose-id"})["event"]["preview"],"A short summary. A second sentence.")
                self.assertEqual(owner({"op":"trace","id":a["id"],"eventID":"legacy-id"})["event"]["preview"],"Legacy readable body. More details follow.")
                self.assertEqual(owner({"op":"trace","id":d["id"],"eventID":"@dot:1"})["event"]["preview"],"Dot says hello.")
                code_reply = owner({"op":"trace","id":d["id"],"eventID":"dot-code"})["event"]
                self.assertEqual(code_reply["dotReplyPreview"],"Code (swift): let answer = 42")
                self.assertEqual(code_reply["dotReplyAt"],"2026-11-12T20:00:01Z")
                self.assertEqual(code_reply["preview"],"Please review this request.")
                job_reply = owner({"op":"trace","id":d["id"],"eventID":"dot-job"})["event"]
                self.assertEqual(job_reply["dotReplyPreview"],"Example item saved")
                self.assertNotIn("opaque record text",json.dumps(job_reply))
                self.assertNotIn("dotReplyPreview",owner({"op":"trace","id":d["id"],"eventID":"dot-unknown"})["event"])
                self.assertEqual(owner({"op":"trace","id":d["id"],"eventID":"dot-blocked"})["event"]["dotReplyPreview"],"Request blocked")
                self.assertEqual(owner({"op":"trace","id":d["id"],"eventID":"dot-potential"})["event"]["dotReplyPreview"],"Needs review. Provide more context.")
                self.assertNotIn("dotReplyPreview",owner({"op":"trace","id":d["id"],"eventID":"dot-secret"})["event"])
                result_reply = owner({"op":"trace","id":d["id"],"eventID":"dot-result"})["event"]
                self.assertEqual(result_reply["dotReplyPreview"],"Example result returned")
                self.assertNotIn("opaque result text",json.dumps(result_reply))
                self.assertNotIn("@wake:",owner({"op":"trace","id":b["id"],"eventID":"wake-text-id"})["event"]["preview"])
                wake_trace = owner({"op":"trace","id":a["id"],"eventID":"@wake:fixture"})["event"]
                self.assertEqual(wake_trace["preview"],"Session wake check")
                self.assertEqual(wake_trace["deliveryMeaning"],"Authenticated connection acknowledged wake check; AI turn not independently proven")
                self.assertNotIn("sensitive",json.dumps(wake_trace))
            finally:
                probe.close()

    def test_all_retained_pages_stable_and_connection_specific(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            first = Probe(directory)
            alpha = first.command({"op": "pair", "name": "Fixture A"})
            beta = first.command({"op": "pair", "name": "Fixture B"})
            first.close()
            with sqlite3.connect(directory / "ledger.sqlite") as db:
                for number in range(137):
                    event_id = f"event-{number:03d}"
                    event = {"service": alpha["id"], "id": event_id,
                             "payload": base64.b64encode(b"{}").decode(), "state": "queued",
                             "stages": [{"state": "queued", "at": "2026-11-12T20:00:00Z", "basis": "broker_recorded"}]}
                    db.execute("INSERT INTO events(service,id,body) VALUES(?,?,?)",
                               (alpha["id"], event_id, json.dumps(event)))
                    if number % 9 == 0:
                        other = dict(event, service=beta["id"], id=f"other-{number:03d}")
                        db.execute("INSERT INTO events(service,id,body) VALUES(?,?,?)",
                                   (beta["id"], other["id"], json.dumps(other)))
            probe = Probe(directory)
            try:
                recent = probe.command({"op": "trace_page", "id": alpha["id"], "limit": 10})
                self.assertEqual(recent["ids"], [f"event-{n:03d}" for n in range(136, 126, -1)])
                self.assertEqual(recent["total"], 137)
                pages = []
                before = None
                through = None
                while True:
                    cmd = {"op": "trace_page", "id": alpha["id"], "limit": 50}
                    if before is not None:
                        cmd.update(before=before, through=through)
                    page = probe.command(cmd)
                    pages.append(page)
                    through, before = page["through"], page["next"]
                    if len(pages) == 1:
                        body = json.dumps({"id": "event-137", "payload": {"fixture": True}}).encode()
                        request = urllib.request.Request(probe.endpoint + "/v1/services/" + alpha["id"] + "/events", body,
                            {"Content-Type": "application/json", "Authorization": "Bearer " + alpha["secret"]})
                        with urllib.request.urlopen(request, timeout=5) as response:
                            self.assertEqual(response.status, 202)
                    if before is None:
                        break
                self.assertEqual([len(page["ids"]) for page in pages], [50, 50, 37])
                self.assertEqual([id for page in pages for id in page["ids"]],
                                 [f"event-{n:03d}" for n in range(136, -1, -1)])
                self.assertTrue(all(page["total"] == 137 for page in pages))
                self.assertEqual(probe.command({"op": "trace_page", "id": beta["id"]})["total"], 16)
                refreshed = probe.command({"op": "trace_page", "id": alpha["id"], "limit": 10})
                self.assertEqual(refreshed["ids"][0], "event-137")
                self.assertEqual(refreshed["total"], 138)
            finally:
                probe.close()


if __name__ == "__main__":
    unittest.main()
