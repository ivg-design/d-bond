"""Production tunnel channel tests, using temporary owner state and a synthetic callback."""
import base64
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
import tempfile
import unittest

import httpx

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE / "vendor")]
import core
import host
import tunnel
from dots_bridge.config import Settings
from dots_bridge.models import MessageInput

CALLBACK = "https://connectors.api.openai.com/fixture/not-a-live-endpoint"
ID_A = "tunnel_" + "a" * 32
ID_B = "tunnel_ab12_" + "b" * 32
STUB_SECRET = "whsec_" + base64.b64encode(b"X" * 32).decode()


class TunnelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.status = host.configure_tunnel(self.root, ID_A)
        self.settings = Settings.model_validate(host.read_config(self.root))
        self.key = tunnel.credential_for(self.root)
        self.callback_requests = 0

    async def deliver(self, url, body, headers):
        self.assertEqual(url, CALLBACK)
        self.callback_requests += 1
        payload = json.loads(body)
        if payload.get("type") == "verification":
            return httpx.Response(200, json={"challenge": payload["challenge"]})
        return httpx.Response(204)

    @asynccontextmanager
    async def running(self):
        bridge = core.DotInbox(self.settings, webhook_post=self.deliver)
        server = tunnel.create_app(self.settings, bridge, self.key)
        try:
            async with server.protocol_lifespan():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server),
                        base_url=f"http://127.0.0.1:{tunnel.PORT}", headers={
                            "Accept": "application/json, text/event-stream", tunnel.HEADER: self.key, tunnel.ID_HEADER: ID_A}) as client:
                    yield bridge, client
        finally:
            await bridge.close()

    def test_provision_without_public_origin_and_preserve_existing_identity(self):
        self.assertTrue(self.status["tunnel_configured"])
        self.assertEqual(self.status["public_url"], "")
        self.assertEqual(self.status["mcp_url"], "")
        before = (self.root / "bridge.toml").read_bytes()
        host.configure_tunnel(self.root, ID_A)
        self.assertEqual(before, (self.root / "bridge.toml").read_bytes())
        self.assertEqual(self.key, tunnel.credential_for(self.root))
        self.assertFalse((self.root / tunnel.KEY_FILENAME).stat().st_mode & 0o077)
        host.configure(self.root, "https://mcp.example", "https://chatgpt.com/fixture")
        public = host.read_config(self.root)
        info = host.configure_tunnel(self.root, ID_A)
        self.assertEqual(host.read_config(self.root), public)
        for name in ("owner_key", "api_key", "storage_key", "owner_user_id"):
            self.assertEqual(public[name], getattr(self.settings, name))
        for secret in (self.key, public["api_key"], public["owner_key"], public["storage_key"]):
            self.assertNotIn(secret, json.dumps(info))
        (self.root / tunnel.KEY_FILENAME).chmod(0o644)
        with self.assertRaises(ValueError):
            tunnel.credential_for(self.root)

    async def rpc(self, client, version, method, params, *, expected=200):
        headers = {"Mcp-Protocol-Version": version}
        if version == tunnel.PROTOCOL_VERSION:
            headers["Mcp-Method"] = method
            if "name" in params:
                headers["Mcp-Name"] = params["name"]
            params = {**params, "_meta": {"io.modelcontextprotocol/protocolVersion": version,
                                         "io.modelcontextprotocol/clientCapabilities": {}}}
        response = await client.post("/mcp", headers=headers,
                                     json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    @staticmethod
    def subscription(url=CALLBACK):
        return {"name": "app.message.created", "arguments": {"channel": "d-bond"},
                "delivery": {"mode": "webhook", "url": url, "secret": STUB_SECRET}}

    async def test_only_dedicated_mcp_route_is_reachable(self):
        async with self.running() as (_, client):
            for path in ("/", "/api/messages", "/api/subscriptions", "/api/host-stop", "/token", "/mcp?query=1",
                         "/.well-known/oauth-protected-resource/mcp"):
                self.assertEqual((await client.post(path)).status_code, 404)
            for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"):
                discovery = await client.get(path)
                self.assertEqual(discovery.status_code, 404)
                self.assertEqual(discovery.text, "Not Found")
            for headers, code in [({tunnel.HEADER: "x" * 64}, 401), ({"Origin": "https://untrusted.example"}, 403),
                                  ({"Host": "localhost"}, 403)]:
                self.assertEqual((await client.post("/mcp", json={}, headers=headers)).status_code, code)
            self.assertEqual((await client.post("/mcp", json={}, headers=[(tunnel.HEADER, self.key)] * 2)).status_code, 401)
            self.assertEqual((await client.post("/mcp", content=b"x" * 400000)).status_code, 413)
            for uri in ("https://example.org/fake", "https://connectors.api.openai.com.invalid/fake"):
                result = await self.rpc(client, tunnel.PROTOCOL_VERSION, "events/subscribe", self.subscription(uri), expected=400)
                self.assertIn("error", result)
            self.assertEqual(self.callback_requests, 0)

    async def test_replacement_clears_proof_and_fences_older_client_and_host(self):
        original = host.read_config(self.root)
        generation = tunnel.current_binding(self.root).generation
        async with self.running() as (bridge, client):
            await self.rpc(client, tunnel.PROTOCOL_VERSION, "events/subscribe", self.subscription())
            subscribed = await bridge.list_subscriptions()
            self.assertTrue(await bridge.tunnel_subscribed())
            host.configure_tunnel(self.root, ID_A)
            self.assertEqual(generation, tunnel.current_binding(self.root).generation)
            self.assertTrue(await bridge.tunnel_subscribed())
            host.configure_tunnel(self.root, ID_B)
            self.assertFalse(await bridge.tunnel_subscribed())
            replacement_key = tunnel.credential_for(self.root)
            self.assertNotEqual(self.key, replacement_key)
            self.assertEqual(host.read_config(self.root), original)
            self.assertEqual(subscribed, await bridge.list_subscriptions())
            for headers in ({}, {tunnel.ID_HEADER: ID_B, tunnel.HEADER: replacement_key}):
                self.assertEqual((await client.post("/mcp", headers=headers, json={})).status_code, 401)
            # Re-selecting A needs its own fresh grant even with the same callback ID.
            host.configure_tunnel(self.root, ID_A)
            self.assertNotEqual(tunnel.current_binding(self.root).generation, generation)
            self.assertFalse(await bridge.tunnel_subscribed())
        self.key = tunnel.credential_for(self.root)
        async with self.running() as (bridge, client):
            self.assertFalse(await bridge.tunnel_subscribed())
            self.assertEqual((await client.post("/mcp", headers={tunnel.ID_HEADER: ID_B}, json={})).status_code, 401)
            await self.rpc(client, tunnel.PROTOCOL_VERSION, "events/subscribe", self.subscription())
            self.assertTrue(await bridge.tunnel_subscribed())

    async def test_protocols_full_tools_persistence_attribution_and_scoped_push(self):
        versions = ("2025-03-26", "2025-06-18", "2025-11-25", tunnel.PROTOCOL_VERSION)
        async with self.running() as (bridge, client):
            for version in versions:
                if version == tunnel.PROTOCOL_VERSION:
                    discovery = await self.rpc(client, version, "server/discover", {})
                else:
                    discovery = await self.rpc(client, version, "initialize", {"protocolVersion": version,
                        "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}})
                self.assertIn("events", discovery["result"]["capabilities"])
                events = await self.rpc(client, version, "events/list", {})
                self.assertEqual(events["result"]["events"][0]["name"], "app.message.created")
                tools = (await self.rpc(client, version, "tools/list", {}))["result"]["tools"]
                self.assertEqual({t["name"] for t in tools}, {"get_current_user", "get_message", "reply_to_app",
                    "report_progress", "cancel_request", "send_to_app", "acknowledge_request"})
                self.assertTrue(all(t["securitySchemes"] == [{"type": "noauth"}] for t in tools))
            established = (await self.rpc(client, versions[1], "events/subscribe", self.subscription()))["result"]
            self.assertTrue(await bridge.tunnel_subscribed())
            request = await bridge.submit(MessageInput(channel="d-bond", text="Synthetic message"))
            self.assertTrue(await bridge.process_next())
        async with self.running() as (bridge, client):
            self.assertTrue(await bridge.tunnel_subscribed())
            args = {"request_id": request["request_id"]}
            for name, extra in (("get_message", {}), ("acknowledge_request", {}),
                                ("report_progress", {"text": "working", "idempotency_key": "test-progress"}),
                                ("reply_to_app", {"text": "returned"})):
                data = await self.rpc(client, versions[-1], "tools/call", {"name": name, "arguments": {**args, **extra}})
                self.assertFalse(data["result"].get("isError"))
            message = await bridge.get_message(args["request_id"])
            self.assertEqual(message["reply"], "returned")
            self.assertEqual(message["subscription_id"], established["id"])
            recipient = "11d11fce-550c-4aac-9991-63af7baa785a"
            pushed = await self.rpc(client, versions[-1], "tools/call", {"name": "send_to_app", "arguments": {
                "channel": "d-bond", "text": "addressed", "metadata": {"recipient_caller_id": "d-bond",
                "recipient_conversation_id": recipient}, "idempotency_key": "test-push"}})
            self.assertFalse(pushed["result"].get("isError"))
            self.assertEqual((await bridge.list_received(channel="d-bond"))[-1]["metadata"]["recipient_conversation_id"], recipient)
            # Normal public-host renewal of even the exact same callback must not look tunnel verified.
            await bridge.subscribe(self.subscription(), self.settings.owner_user_id)
            self.assertFalse(await bridge.tunnel_subscribed())
            await self.rpc(client, versions[-1], "events/subscribe", self.subscription())
            self.assertTrue(await bridge.tunnel_subscribed())
            unsubscribe = self.subscription()
            del unsubscribe["delivery"]["secret"]
            self.assertNotIn("error", await self.rpc(client, versions[-1], "events/unsubscribe", unsubscribe))
            self.assertFalse(await bridge.tunnel_subscribed())


if __name__ == "__main__":
    unittest.main()
