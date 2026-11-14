"""Isolated, opt-in OpenAI tunnel probe for the actual D-Bond Dot host."""
import argparse
import asyncio
import base64
import fcntl
import getpass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
import sqlite3
import sys
import time
from contextlib import asynccontextmanager, ExitStack
from datetime import datetime
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

HOST = Path(__file__).resolve().parents[2] / "adapters/dot-host"
sys.path[:0] = [str(HOST), str(HOST / "vendor")]
import host
from core import DotInbox, create_app
from dots_bridge.config import Settings
from dots_bridge.mcp import PROTOCOL_VERSION
from dots_bridge.webhooks import post_webhook, validate_url

sys.path.insert(0, str(Path(__file__).resolve().parent))
import broker

PORT = 18779
API_PORT = 18780
STATE = Path.home() / "Library/Application Support/D-Bond Tunnel Probe"
ORIGIN = "http://127.0.0.1:18779"
CALLBACK_HOST = "connectors.api.openai.com"
TOOLS = {"get_current_user", "get_message", "acknowledge_request", "reply_to_app"}
METHODS = {"server/discover", "initialize", "notifications/initialized", "ping",
           "tools/list", "tools/call", "events/list", "events/subscribe", "events/unsubscribe"}


class ProbeError(Exception):
    """Operator-safe local failure; messages never contain external input."""


def settings_for(root):
    host.private_dir(root)
    config_file = root / "probe.json"
    if not config_file.exists():
        if list(root.iterdir()):
            raise ProbeError("Choose a new empty directory for this probe")
        config = {"owner_user_id": "tunnel-probe-" + uuid4().hex,
                  "owner_key": secrets.token_urlsafe(32), "api_key": secrets.token_urlsafe(32),
                  "storage_key": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
                  "ingress": secrets.token_urlsafe(48)}
        # Publish complete credentials before another process can use this state.
        temp = root / (".init-" + uuid4().hex)
        try:
            with temp.open("x") as stream:
                json.dump(config, stream)
            os.link(temp, config_file)
        finally:
            temp.unlink(missing_ok=True)
    fd = os.open(config_file, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 4096:
            raise ValueError("Probe credentials require an owner-only regular file")
        config = json.load(stream)
    ingress = config.pop("ingress")
    settings = Settings(**config, db_path=broker.database_path(root), public_base_url=ORIGIN,
                        port=PORT, client_id="dbond-tunnel-probe", redirect_uri=ORIGIN + "/unused")
    broker.prepare(root, settings)
    return settings, ingress


async def openai_webhook(url, body, headers):
    if validate_url(url).hostname != CALLBACK_HOST:
        raise ValueError("Unexpected callback host")
    return await post_webhook(url, body, headers)


class LocalGrant:
    """Real PKCE consent for the isolated in-process host's scoped tools."""
    def __init__(self, client, settings):
        self.client = client
        self.settings = settings
        self.deadline = 0
        self.token = ""
        self.lock = asyncio.Lock()

    async def get(self):
        async with self.lock:
            if time.monotonic() < self.deadline:
                return self.token
            verifier = secrets.token_urlsafe(48)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            auth = await self.client.get("/authorize", params={
                "response_type": "code", "client_id": self.settings.client_id, "redirect_uri": self.settings.redirect_uri,
                "resource": ORIGIN + "/mcp", "scope": "events:read messages:reply",
                "code_challenge": challenge, "code_challenge_method": "S256", "state": secrets.token_hex(16)})
            if auth.status_code != 302:
                raise RuntimeError("Probe grant initialization failed")
            location = auth.headers["location"]
            rid = parse_qs(urlsplit(location).query)["request_id"][0]
            (await self.client.get(location)).raise_for_status()
            consent = await self.client.post("/consent", headers={"Origin": ORIGIN},
                                            data={"request_id": rid, "owner_key": self.settings.owner_key})
            if consent.status_code != 302:
                raise RuntimeError("Probe owner approval failed")
            code = parse_qs(urlsplit(consent.headers["location"]).query)["code"][0]
            token = await self.client.post("/token", data={"grant_type": "authorization_code", "code": code,
                "client_id": self.settings.client_id, "redirect_uri": self.settings.redirect_uri,
                "resource": ORIGIN + "/mcp", "code_verifier": verifier})
            token.raise_for_status()
            values = token.json()
            self.token = values["access_token"]
            self.deadline = time.monotonic() + values["expires_in"] - 30
            return self.token


def build_server(root, settings, ingress, bridge=None):
    bridge = bridge or DotInbox(settings, webhook_post=openai_webhook)
    actual = create_app(settings, bridge=bridge)
    boot = uuid4().hex

    @asynccontextmanager
    async def lifespan(app):
        async with actual.router.lifespan_context(actual):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=actual), base_url=ORIGIN) as local:
                app.state.client = local
                app.state.grant = LocalGrant(local, settings)
                await app.state.grant.get()

                def tables(db):
                    db.execute("CREATE TABLE IF NOT EXISTS probe_calls (request_id TEXT, method TEXT, "
                               "boot TEXT, tunnel_run TEXT, PRIMARY KEY(request_id,method))")
                    db.execute("CREATE TABLE IF NOT EXISTS probe_subscriptions (change TEXT, boot TEXT, at REAL)")
                    db.execute("CREATE TABLE IF NOT EXISTS probe_requests (request_id TEXT PRIMARY KEY, "
                               "marker TEXT, boot TEXT, subscription_id TEXT, subscription_changes INTEGER)")
                await bridge.store.run(tables)
                await bridge.store.run(lambda db: db.execute("INSERT OR REPLACE INTO dbond_host VALUES ('probe_boot',?)", (boot,)))
                # The Swift broker does its initial native status check over the second listener.
                # Start it after uvicorn has installed both sockets.
                task = asyncio.create_task(asyncio.to_thread(broker_context.__enter__))
                def check_startup(done):
                    if not done.cancelled() and done.exception() is not None:
                        # No working Swift source means this host cannot perform the test.
                        print('{"error":"swift_broker_startup_failed"}', file=sys.stderr, flush=True)
                        app.state.broker_failed = True
                        app.state.stop()
                task.add_done_callback(check_startup)
                try:
                    yield
                finally:
                    await task
                    await asyncio.to_thread(broker_context.__exit__, None, None, None)

    broker_context = broker.start_broker(root)

    async def rpc(request: Request):
        if not secrets.compare_digest(request.headers.get("x-dbond-probe", "").encode(), ingress.encode()):
            return Response(status_code=401)
        tunnel_run = request.headers.get("x-dbond-tunnel-run", "")
        if not re.fullmatch(r"[0-9a-f]{32}", tunnel_run):
            return Response(status_code=401)
        if request.headers.get("origin") or request.headers.get("host") != f"127.0.0.1:{PORT}":
            return Response(status_code=403)
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            return Response(status_code=415)
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 262144:
                return Response(status_code=413)
            raw.extend(chunk)
        try:
            body = json.loads(raw)
            if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
                return Response(status_code=400)
            method = body.get("method")
            params = body.get("params", {})
            if not isinstance(method, str) or not isinstance(params, dict):
                return Response(status_code=400)
        except (ValueError, UnicodeError, RecursionError):
            return Response(status_code=400)
        if method == "tools/call" and not isinstance(params.get("arguments", {}), dict):
            return Response(status_code=400)
        if method not in METHODS or (method == "tools/call" and (not isinstance(params.get("name"), str) or params["name"] not in TOOLS)):
            return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"),
                                 "error": {"code": -32601, "message": "Method unavailable on this probe"}})
        # The host's Events extension is registered for MCP 2.0. Forwards
        # preserve the caller's protocol metadata and never invent a cloud subscription.
        local_headers = {key: value for key, value in request.headers.items()
                         if key.lower() in {"mcp-protocol-version", "mcp-method", "mcp-name", "accept"}}
        local_headers.setdefault("accept", "application/json, text/event-stream")
        local_headers["authorization"] = "Bearer " + await request.app.state.grant.get()
        response = await request.app.state.client.post("/mcp", headers=local_headers, json=body)
        if response.status_code != 200 or not response.content:
            return Response(status_code=response.status_code)
        try:
            data = response.json()
        except ValueError:
            return Response(status_code=502)
        result = data.get("result", {})
        if not isinstance(result, dict):
            return JSONResponse(data)
        if method in {"events/subscribe", "events/unsubscribe"} and "result" in data:
            await bridge.store.run(lambda db: db.execute("INSERT INTO probe_subscriptions VALUES (?,?,?)", (method, boot, time.time())))
        if "tools" in result and method == "tools/list":
            visible = []
            for item in result["tools"]:
                if item["name"] in TOOLS:
                    schemes = [{"type": "noauth"}]
                    visible.append({**item, "securitySchemes": schemes,
                                    "_meta": {**item.get("_meta", {}), "securitySchemes": schemes}})
            result["tools"] = visible
        if (method == "tools/call" and params.get("name") in {"acknowledge_request", "reply_to_app"}
                and "result" in data and not result.get("isError")):
            rid = params.get("arguments", {}).get("request_id")
            await bridge.store.run(lambda db: db.execute("INSERT OR IGNORE INTO probe_calls VALUES (?,?,?,?)",
                                                        (rid, params["name"], boot, tunnel_run)))
        return JSONResponse(data)

    ingress_app = Starlette(routes=[Route("/mcp", rpc, methods=["POST"])], lifespan=lifespan)

    async def split(scope, receive, send):
        if scope["type"] == "http" and scope["server"][1] == API_PORT:
            allowed = {("GET", "/api/subscriptions"), ("POST", "/api/messages")}
            if (scope["method"], scope["path"]) in allowed:
                return await actual(scope, receive, send)
            return await Response(status_code=404)(scope, receive, send)
        return await ingress_app(scope, receive, send)
    split.state = ingress_app.state
    return split


def local_time(value):
    if value:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    return None


async def command(args, root, settings, ingress, bridge_override=None):
    if args.command == "serve":
        with ExitStack() as stack:
            lock = stack.enter_context((root / "host.lock").open("a"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            sockets = []
            for port in (PORT, API_PORT):
                bound = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
                bound.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                bound.bind(("127.0.0.1", port))
                bound.listen(128)
                sockets.append(bound)
            host.verify_vendor()
            app = build_server(root, settings, ingress, bridge_override)
            server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT,
                                    access_log=False, log_config=None, proxy_headers=False))
            app.state.stop = lambda: setattr(server, "should_exit", True)
            await server.serve(sockets=sockets)
            if not server.started or getattr(app.state, "broker_failed", False):
                raise RuntimeError("Host failed to start")
            return
    if args.command == "tunnel":
        if not re.fullmatch(r"tunnel_[A-Za-z0-9_-]{6,160}", args.tunnel_id):
            raise ValueError("Invalid tunnel ID")
        binary = shutil.which("tunnel-client")
        if not binary:
            raise RuntimeError("Install OpenAI tunnel-client first")
        env = {k: os.environ[k] for k in ("HOME", "PATH", "TMPDIR") if k in os.environ}
        key = os.environ.get("CONTROL_PLANE_API_KEY") or getpass.getpass("Restricted OpenAI tunnel runtime key: ")
        if not key:
            raise ValueError("Runtime key required")
        env.update(CONTROL_PLANE_API_KEY=key, DBOND_PROBE_INGRESS=ingress, DBOND_PROBE_TUNNEL_RUN=uuid4().hex)
        os.execve(binary, [binary, "run", "--control-plane.tunnel-id", args.tunnel_id, "--mcp.server-url",
            ORIGIN + "/mcp", "--mcp.extra-headers", "X-DBond-Probe: env:DBOND_PROBE_INGRESS",
            "--mcp.extra-headers", "X-DBond-Tunnel-Run: env:DBOND_PROBE_TUNNEL_RUN",
            "--health.listen-addr", "127.0.0.1:0", "--health.url-file", str(root / "tunnel-health.url"),
            "--log.format", "json", "--log.level", "warn", "--log.http-raw-unsafe=false"], env)
    bridge = DotInbox(settings, webhook_post=openai_webhook)
    try:
        subs = await bridge.list_subscriptions()
        if args.command == "status":
            try:
                changes = await bridge.store.run(lambda db: db.execute("SELECT count(*) FROM probe_subscriptions").fetchone()[0])
            except sqlite3.OperationalError:
                changes = 0
            return {"verified_subscriptions": len(subs), "subscription_changes": changes,
                    "subscription_id": subs[0]["id"] if len(subs) == 1 else None}
        if args.command == "send":
            if len(subs) != 1:
                raise ProbeError("No active, verified Dot subscription on the probe")
            marker = "DBOND-" + secrets.token_hex(8)
            boot = await bridge.store.run(lambda db: db.execute("SELECT value FROM dbond_host WHERE key='probe_boot'").fetchone())
            if not boot:
                raise ProbeError("Start the probe host first")
            changes = await bridge.store.run(lambda db: db.execute("SELECT count(*) FROM probe_subscriptions").fetchone()[0])
            prompt = f"D-Bond private tunnel wake check. Acknowledge this request with this connector, then reply_to_app with exactly {marker}."
            rid = await asyncio.to_thread(broker.submit, root, prompt, marker)
            await bridge.store.run(lambda db: db.execute("INSERT INTO probe_requests VALUES (?,?,?,?,?)",
                (rid, marker, boot[0], subs[0]["id"], changes)))
            row = await bridge.get_message(rid)
            return {"request_id": row["request_id"], "sent_local": local_time(row["created_at"])}
        async def receipt(rid):
            row = await bridge.get_message(rid)
            calls = await bridge.store.run(lambda db: {r[0]: (r[1], r[2]) for r in db.execute(
                "SELECT method,boot,tunnel_run FROM probe_calls WHERE request_id=?", (rid,))})
            entry = await bridge.store.run(lambda db: db.execute("SELECT marker,boot,subscription_id,subscription_changes "
                "FROM probe_requests WHERE request_id=?", (rid,)).fetchone())
            if entry is None:
                raise ProbeError("No test run matches that request ID")
            marker, gen, sid, changes = entry
            ack = calls.get("acknowledge_request")
            reply = calls.get("reply_to_app")
            broker_received = await asyncio.to_thread(broker.receipt, root, rid, marker)
            return {"request_id": rid, "subscription_id": row["subscription_id"], "host_boot": gen,
                "tunnel_run": ack[1] if ack else None, "subscription_changes": changes, "paired_service_received": broker_received,
                "sent_local": local_time(row["created_at"]), "callback_http": row["status_code"],
                "ack_local": local_time(row["started_at"]), "reply_local": local_time(row["replied_at"]),
                "ack_seconds": round((datetime.fromisoformat(row["started_at"]) - datetime.fromisoformat(row["created_at"])).total_seconds(), 1) if row["started_at"] else None,
                "verified": bool(row["delivery_status"] == "accepted" and row["started_at"] and row["reply"] == marker
                            and row["subscription_id"] == sid and ack and ack == reply and ack[0] == gen and broker_received)}
        if args.command == "receipt":
            return await receipt(args.request_id)
        before, after = await receipt(args.before), await receipt(args.after)
        verified = (before["verified"] and after["verified"] and before["request_id"] != after["request_id"]
                    and before["host_boot"] != after["host_boot"] and before["subscription_id"] == after["subscription_id"]
                    and before["tunnel_run"] != after["tunnel_run"] and before["subscription_changes"] == after["subscription_changes"]
                    and len(subs) == 1 and subs[0]["id"] == after["subscription_id"])
        current_changes = await bridge.store.run(lambda db: db.execute("SELECT count(*) FROM probe_subscriptions").fetchone()[0])
        verified = verified and current_changes == after["subscription_changes"]
        return {"verified_across_restart": verified, "before": before, "after": after}
    finally:
        await bridge.close()


def main():
    os.umask(0o077)
    logging.disable(logging.CRITICAL)  # HTTP errors can include callback identities and query strings.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=STATE)
    sub = parser.add_subparsers(dest="command", required=True)
    for cmd in ("serve", "status", "send"):
        sub.add_parser(cmd)
    sub.add_parser("tunnel").add_argument("tunnel_id")
    sub.add_parser("receipt").add_argument("request_id")
    verify = sub.add_parser("verify")
    verify.add_argument("before")
    verify.add_argument("after")
    args = parser.parse_args()
    root = args.state.absolute()
    try:
        settings, ingress = settings_for(root)
        result = asyncio.run(command(args, root, settings, ingress))
        if result is not None:
            print(json.dumps(result))
        if args.command == "verify" and not result["verified_across_restart"]:
            raise SystemExit(2)
    except ProbeError as exc:
        print(json.dumps({"action": args.command, "reason": str(exc)}), file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        # Third-party exceptions can embed credentials or private destinations.
        print(json.dumps({"error": type(exc).__name__, "action": args.command}), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
