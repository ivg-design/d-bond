"""Send ONE authenticated, already persisted job version to the local broker.

Call from your existing webhook watcher's dispatch boundary. No listener, poll,
production configuration change, or proposal submission is performed here.
"""
import argparse
import hashlib
import json
import pathlib
import sys
import urllib.error

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "adapters/cli"))
import dbond


def envelope(job_id, revision, job):
    if not all(isinstance(x, str) and x.strip() and len(x) <= 256 for x in (job_id, revision)):
        raise ValueError("Specify a stable source job ID and revision (max 256 characters each).")
    if not isinstance(job, dict):
        raise ValueError("Expected one complete job JSON object.")
    key = json.dumps(["vollna", job_id, revision], ensure_ascii=False, separators=(",", ":")).encode()
    return {"id": "vollna-" + hashlib.sha256(key).hexdigest(),
            "payload": {"kind": "job.received", "job_id": job_id, "revision": revision, "job": job}}


def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--alias", default="vollna-intake")
    args.add_argument("--job-id", required=True)
    args.add_argument("--revision", required=True)
    args.add_argument("--preview", action="store_true", help="validate locally and print the event ID without sending")
    a = args.parse_args()
    try:
        raw = sys.stdin.buffer.read(192 * 1024 + 1)
        if len(raw) > 192 * 1024:
            raise ValueError("Job exceeds the local envelope limit; send an authenticated source reference instead.")
        event = envelope(a.job_id, a.revision, json.loads(raw))
        if len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()) > 192 * 1024:
            raise ValueError("Job and envelope exceed 192 KiB; no data sent.")
        if a.preview:
            print(json.dumps({"id": event["id"], "sent": False}))
            return
        p = dbond._read_pairing(a.alias)
        status, receipt = dbond._request(p, "POST", p["endpoint"], event)
        if status not in (200, 202) or receipt.get("id") != event["id"]:
            raise ValueError("Could not confirm local storage. Reconcile this event before retry.")
        print(json.dumps({"id": event["id"], "stored": True, "duplicate": status == 200}))
    except (UnicodeError, ValueError, OSError, urllib.error.URLError) as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
