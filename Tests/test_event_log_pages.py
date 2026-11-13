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
