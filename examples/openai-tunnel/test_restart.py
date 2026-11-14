"""Offline restart acceptance. The callback receiver and tunnel HTTP caller are simulated."""
import asyncio
import base64
from contextlib import contextmanager
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe
from core import DotInbox

TEST_SECRET = "whsec_" + base64.b64encode(b"s" * 32).decode()
TEST_DESTINATION = "https://offline-receiver.invalid/test"


async def fixture_callback(url, data, headers):
    if url != TEST_DESTINATION:
        raise AssertionError("Unexpected fixture callback URL")
    payload = json.loads(data)
    signed = headers["webhook-id"].encode() + b"." + headers["webhook-timestamp"].encode() + b"." + data
    sig = base64.b64encode(hmac.digest(b"s" * 32, signed, hashlib.sha256)).decode()
    assert hmac.compare_digest("v1," + sig, headers["webhook-signature"])
    if payload.get("type") == "verification":
        return httpx.Response(200, json={"challenge": payload["challenge"]})
    assert payload["name"] == "app.message.created"
    return httpx.Response(204)


@contextmanager
def running(root):
    child = subprocess.Popen([sys.executable, "-I", "-B", __file__, "--offline-host", str(root)])
    try:
        deadline = time.monotonic() + 15
        with httpx.Client(trust_env=False) as client:
            while child.poll() is None and time.monotonic() < deadline:
                try:
                    if client.get(probe.ORIGIN + "/api/subscriptions", timeout=.4).status_code == 404:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(.05)
            else:
                raise AssertionError("Offline host did not start")
        yield
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)
        child.wait(timeout=15)


class RestartTest(unittest.TestCase):
    def test_isolated_host_and_real_swift_sender(self):
        with tempfile.TemporaryDirectory() as d, httpx.Client(trust_env=False, timeout=6) as local:
            root = Path(d)
            settings, ingress = probe.settings_for(root)
            def command(name, **kw):
                return asyncio.run(probe.command(SimpleNamespace(command=name, **kw), root, settings, ingress))
            def rpc(method, params):
                headers = {"X-DBond-Probe": ingress, "X-DBond-Tunnel-Run": tunnel_run,
                           "Mcp-Protocol-Version": probe.PROTOCOL_VERSION, "Mcp-Method": method,
                           "Accept": "application/json, text/event-stream"}
                if "name" in params:
                    headers["Mcp-Name"] = params["name"]
                r = local.post(probe.ORIGIN + "/mcp", headers=headers,
                    json={"jsonrpc": "2.0", "id": 1, "method": method,
                          "params": {**params, "_meta": {"io.modelcontextprotocol/protocolVersion": probe.PROTOCOL_VERSION,
                                     "io.modelcontextprotocol/clientCapabilities": {}}}})
                self.assertEqual(r.status_code, 200)
                self.assertIn("result", r.json(), r.text)
                value = r.json()["result"]
                self.assertFalse(value.get("isError"), value)
                return value
            requests = []
            for index in range(2):
                with running(root):
                    tunnel_run = uuid4().hex
                    self.assertEqual(local.get(probe.ORIGIN + "/api/subscriptions").status_code, 404)
                    self.assertEqual(local.post(probe.ORIGIN + "/mcp", json={}).status_code, 401)
                    self.assertEqual(local.get("http://127.0.0.1:18780/api/subscriptions").status_code, 401)
                    self.assertIn("events", rpc("server/discover", {})["capabilities"])
                    self.assertEqual({t["name"] for t in rpc("tools/list", {})["tools"]}, probe.TOOLS)
                    self.assertEqual(rpc("events/list", {})["events"][0]["name"], "app.message.created")
                    if index == 0:
                        rpc("events/subscribe", {"name": "app.message.created", "arguments": {"channel": "d-bond"},
                            "delivery": {"mode": "webhook", "secret": TEST_SECRET, "url": TEST_DESTINATION}})
                    self.assertEqual(command("status")["subscription_changes"], 1)
                    rid = command("send")["request_id"]
                    requests.append(rid)
                    message = rpc("tools/call", {"name": "get_message", "arguments": {"request_id": rid}})["structuredContent"]
                    marker = message["metadata"]["data"]["probe"]
                    rpc("tools/call", {"name": "acknowledge_request", "arguments": {"request_id": rid}})
                    rpc("tools/call", {"name": "reply_to_app", "arguments": {"request_id": rid, "text": marker}})
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        if command("receipt", request_id=rid)["verified"]:
                            break
                        time.sleep(.1)
                    else:
                        import sqlite3
                        with sqlite3.connect(root / "swift-ledger/ledger.sqlite") as db:
                            receipt_states = [r[0] for r in db.execute("select json_extract(body,'$.state') from receipts")]
                            event_states = [r[0] for r in db.execute("select json_extract(body,'$.state') from events")]
                        self.fail(str((receipt_states, event_states, command("receipt", request_id=rid))))
                    if index:
                        evidence = command("verify", before=requests[0], after=rid)
                        self.assertTrue(evidence["verified_across_restart"], evidence)
                        self.assertFalse(command("verify", before=rid, after=rid)["verified_across_restart"])
            for url in ("https://example.org/callback", "https://connectors.api.openai.com.invalid/callback"):
                with self.assertRaises(ValueError):
                    asyncio.run(probe.openai_webhook(url, b"{}", {}))


if __name__ == "__main__":
    os.umask(0o077)
    if len(sys.argv) == 3 and sys.argv[1] == "--offline-host":
        logging.disable(logging.CRITICAL)
        root = Path(sys.argv[2])
        settings, ingress = probe.settings_for(root)
        bridge = DotInbox(settings, webhook_post=fixture_callback)
        asyncio.run(probe.command(SimpleNamespace(command="serve"), root, settings, ingress, bridge))
    else:
        unittest.main()
