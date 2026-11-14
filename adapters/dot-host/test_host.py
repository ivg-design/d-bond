"""Disposable signed-callback, HTTP/MCP, setup and restart compatibility checks."""
import base64
from contextlib import closing
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from urllib.parse import parse_qs, urlsplit
from pathlib import Path
from unittest.mock import patch
from fastmcp.server.auth import StaticTokenVerifier

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE / "vendor")]
import httpx
import host
from core import DotInbox, create_app
from dots_bridge.config import Settings
from dots_bridge.errors import BridgeError
from dots_bridge.models import MessageInput

RECIPIENT = "11111111-2222-3333-4444-555555555555"


class HostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "private"
        host.configure(self.root, "https://connector.example", "https://chatgpt.com/connector/oauth/callback")
        self.settings = Settings.model_validate(host.read_config(self.root))
        self.calls = []
        self.secret = "whsec_" + base64.b64encode(b"s" * 32).decode()
        self.callback_code = 204
        self.bridge = DotInbox(self.settings, webhook_post=self.callback)

    async def asyncTearDown(self):
        await self.bridge.close()
        self.temporary.cleanup()

    async def callback(self, url, body, headers):
        signed = headers["webhook-id"].encode() + b"." + headers["webhook-timestamp"].encode() + b"." + body
        expected = "v1," + base64.b64encode(hmac.digest(b"s" * 32, signed, hashlib.sha256)).decode()
        self.assertIn(expected, headers["webhook-signature"].split())
        payload = json.loads(body)
        self.calls.append((body, headers))
        if payload.get("type") == "verification":
            return httpx.Response(200, json={"challenge": payload["challenge"]})
        return httpx.Response(self.callback_code)

    async def subscribe(self, url="https://callback.example/events"):
        return await self.bridge.subscribe({"name": "app.message.created", "arguments": {"channel": "d-bond"},
            "delivery": {"mode": "webhook", "url": url, "secret": self.secret}}, self.settings.owner_user_id)

    async def test_queue_ack_reply_addressed_push_and_restart_keep_exact_identity(self):
        submitted = await self.bridge.submit(MessageInput(channel="d-bond", text="A question", metadata={
            "caller_id": "d-bond", "conversation_id": RECIPIENT, "dbond_event": "fixture"}, idempotency_key="dbond1_fixture"))
        self.assertFalse(await self.bridge.process_next())
        await self.subscribe()
        self.assertTrue(await self.bridge.process_next())
        self.assertEqual((await self.bridge.get_message(submitted["request_id"]))["delivery_status"], "accepted")
        self.assertIsNone((await self.bridge.get_message(submitted["request_id"]))["started_at"])
        first = await self.bridge.acknowledge_request(submitted["request_id"])
        second = await self.bridge.acknowledge_request(submitted["request_id"])
        self.assertFalse(first["already_started"])
        self.assertTrue(second["already_started"])
        self.assertEqual(first["started_at"], second["started_at"])
        await self.bridge.reply_to_app(submitted["request_id"], "Exact reply")
        push = await self.bridge.send_to_app("Addressed push", idempotency_key="push-fixture", metadata={
            "recipient_caller_id": "d-bond", "recipient_conversation_id": RECIPIENT})
        await self.bridge.close()
        self.bridge = DotInbox(self.settings, webhook_post=self.callback)
        received = await self.bridge.list_received(channel="d-bond")
        self.assertEqual([row["kind"] for row in received], ["reply", "push"])
        self.assertEqual(received[0]["request_id"], submitted["request_id"])
        self.assertEqual(received[1]["metadata"]["recipient_conversation_id"], RECIPIENT)
        self.assertEqual(push["id"], received[1]["id"])
        again = await self.bridge.send_to_app("Addressed push", idempotency_key="push-fixture", metadata={
            "recipient_caller_id": "d-bond", "recipient_conversation_id": RECIPIENT})
        self.assertEqual(again["id"], push["id"])
        self.assertFalse(await self.bridge.process_next())
        with closing(sqlite3.connect(self.settings.db_path)) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            row = db.execute("SELECT started_at,reply,reply_cancelled FROM messages").fetchone()
            self.assertIsNotNone(row[0])
            self.assertEqual(row[1:], ("Exact reply", 0))

    async def test_callback_410_and_restart_rebind_preserve_message_and_signatures(self):
        await self.subscribe()
        sent = await self.bridge.submit(MessageInput(channel="d-bond", text="Retained", idempotency_key="retained"))
        self.callback_code = 410
        await self.bridge.process_next()
        self.assertEqual(await self.bridge.list_subscriptions(), [])
        await self.bridge.close()
        self.bridge = DotInbox(self.settings, webhook_post=self.callback)
        await self.subscribe("https://callback.example/replaced")
        await self.bridge.store.run(lambda db: db.execute("UPDATE messages SET next_attempt_at=0"))
        self.callback_code = 204
        self.assertTrue(await self.bridge.process_next())
        delivered = [json.loads(body) for body, _ in self.calls if "eventId" in json.loads(body)]
        self.assertEqual([event["eventId"] for event in delivered], [sent["event_id"], sent["event_id"]])
        self.assertEqual((await self.bridge.get_message(sent["request_id"]))["delivery_status"], "accepted")

    async def test_only_d_bond_route_and_explicit_revocation_close_old_work(self):
        with self.assertRaises(BridgeError):
            await self.bridge.submit(MessageInput(channel="inbox", text="Wrong"))
        with self.assertRaises(BridgeError):
            await self.bridge.send_to_app("Wrong", idempotency_key="bad", channel="d-bond", metadata={})
        sent = await self.bridge.submit(MessageInput(channel="d-bond", text="Retained", idempotency_key="retained"))
        await self.bridge.revoke_subscriptions()
        await self.subscribe()
        self.assertFalse(await self.bridge.process_next())
        with self.assertRaises(BridgeError):
            await self.bridge.acknowledge_request(sent["request_id"])

    async def test_authenticated_api_compatibility_and_mcp_ack_tool(self):
        verifier = StaticTokenVerifier(tokens={"isolated-token": {"client_id": "test", "scopes": ["events:read", "messages:reply"]}})
        with patch("dots_bridge.app.PersonalOAuth", return_value=verifier):
            app = create_app(self.settings, bridge=self.bridge, start_worker=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:18769") as client:
                self.assertEqual((await client.get("/api/host-status")).status_code, 401)
                headers = {"Authorization": "Bearer " + self.settings.api_key}
                self.assertEqual((await client.get("/api/subscriptions", headers=headers)).json(),
                                 {"subscriptions": [], "slack_channels": [], "native_channels": []})
                posted = await client.post("/api/messages", headers=headers,
                    json={"channel": "d-bond", "text": "Question", "idempotency_key": "http-fixture"})
                self.assertEqual(posted.status_code, 202)
                request_id = posted.json()["request_id"]
                read = await client.get("/api/messages/" + request_id, headers=headers)
                self.assertIsNone(read.json()["started_at"])
                status = (await client.get("/api/host-status", headers=headers)).json()
                self.assertEqual(status["host"], "d-bond-dot-host/1")
                self.assertEqual(status["store_id"], self.settings.owner_user_id)
                async def rpc(method, params):
                    return await client.post("/mcp", headers={"Authorization": "Bearer isolated-token",
                        "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method,
                        **({"Mcp-Name": params["name"]} if "name" in params else {})},
                        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {**params,
                            "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                      "io.modelcontextprotocol/clientCapabilities": {}}}})
                listed = await rpc("tools/list", {})
                self.assertEqual(listed.status_code, 200, listed.text)
                self.assertIn("acknowledge_request", [tool["name"] for tool in listed.json()["result"]["tools"]])
                ack = await rpc("tools/call", {"name": "acknowledge_request", "arguments": {"request_id": request_id}})
                self.assertEqual(ack.status_code, 200, ack.text)
                self.assertNotEqual(ack.json()["result"].get("isError"), True, ack.text)
                reply = await rpc("tools/call", {"name": "reply_to_app", "arguments": {"request_id": request_id, "text": "HTTP reply"}})
                self.assertNotEqual(reply.json()["result"].get("isError"), True, reply.text)
                push = await rpc("tools/call", {"name": "send_to_app", "arguments": {"channel": "d-bond", "text": "HTTP push",
                    "idempotency_key": "http-push", "metadata": {"recipient_caller_id": "d-bond", "recipient_conversation_id": RECIPIENT}}})
                self.assertNotEqual(push.json()["result"].get("isError"), True, push.text)
                received = (await client.get("/api/received?channel=d-bond", headers=headers)).json()["messages"]
                self.assertEqual([row["kind"] for row in received], ["reply", "push"])

    async def test_real_oauth_consent_and_grant_survive_host_recreation(self):
        verifier = "v" * 43
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        app = create_app(self.settings, bridge=self.bridge, start_worker=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://connector.example") as client:
                authorization = await client.get("/authorize", params={"response_type": "code", "client_id": "d-bond",
                    "redirect_uri": self.settings.redirect_uri, "resource": "https://connector.example/mcp",
                    "scope": "events:read messages:reply", "code_challenge": challenge, "code_challenge_method": "S256", "state": "isolated"})
                self.assertEqual(authorization.status_code, 302)
                consent_url = authorization.headers["location"]
                request_id = parse_qs(urlsplit(consent_url).query)["request_id"][0]
                self.assertEqual((await client.get(consent_url)).status_code, 200)
                consent = await client.post("/consent", headers={"Origin": "https://connector.example"},
                    data={"request_id": request_id, "owner_key": self.settings.owner_key})
                self.assertEqual(consent.status_code, 302)
                code = parse_qs(urlsplit(consent.headers["location"]).query)["code"][0]
                exchanged = await client.post("/token", data={"grant_type": "authorization_code", "code": code,
                    "client_id": "d-bond", "redirect_uri": self.settings.redirect_uri, "code_verifier": verifier,
                    "resource": "https://connector.example/mcp"})
                self.assertEqual(exchanged.status_code, 200)
                token = exchanged.json()["access_token"]
        self.bridge = DotInbox(self.settings, webhook_post=self.callback)
        restarted = create_app(self.settings, bridge=self.bridge, start_worker=False)
        loaded = await restarted.state.auth.load_access_token(token)
        self.assertIsNotNone(loaded)
        self.assertEqual(set(loaded.scopes), {"events:read", "messages:reply"})


class SetupTests(unittest.TestCase):
    def test_reconfigure_preserves_keys_and_status_never_discloses_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "owned"
            first = host.configure(root, "https://connector.example", "https://chatgpt.com/callback")
            before = host.read_config(root)
            second = host.configure(root, "https://changed.example", "https://chatgpt.com/callback")
            after = host.read_config(root)
            for key in ("owner_key", "api_key", "storage_key", "owner_user_id"):
                self.assertEqual(before[key], after[key])
                self.assertNotIn(before[key], json.dumps(first) + json.dumps(second))
            self.assertFalse(first["subscribed"])
            self.assertFalse(first["running"])
            self.assertEqual(os.stat(root / "bridge.toml").st_mode & 0o777, 0o600)

    def test_clean_upstream_is_pinned_and_unchanged(self):
        host.verify_vendor()


if __name__ == "__main__":
    unittest.main()
