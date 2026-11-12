#!/usr/bin/env python3
"""Loopback-only D-Bond fixture backed by the installed Dot Bridge engine."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import signal
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
from cryptography.fernet import Fernet

from dots_bridge.bridge import DotsBridge
from dots_bridge.config import Settings
from dots_bridge.errors import BridgeError
from dots_bridge.models import EVENT_NAME
from dots_bridge.store import Store
from dots_bridge.webhooks import signing_key

CALLBACK_URL = "https://dbond-fixture.invalid/callback"
GENERIC_EVENT_TEXT = "D-Bond fixture notice; acknowledge this request to retrieve its payload."
MAX_BODY = 64 * 1024
OPERATION_TIMEOUT = 10.0


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _verify_signature(body: bytes, headers: dict[str, str], secret: str) -> bool:
    lowered = {key.lower(): value for key, value in headers.items()}
    message_id = lowered.get("webhook-id", "")
    timestamp = lowered.get("webhook-timestamp", "")
    signatures = lowered.get("webhook-signature", "").split()
    if not message_id or not timestamp or not signatures:
        return False
    try:
        if abs(int(timestamp) - int(time.time())) > 300:
            return False
    except (TypeError, ValueError):
        return False
    signed = message_id.encode("utf-8") + b"." + timestamp.encode("ascii") + b"." + body
    expected = base64.b64encode(hmac.digest(signing_key(secret), signed, "sha256")).decode(
        "ascii"
    )
    return any(
        candidate.startswith("v1,") and hmac.compare_digest(candidate[3:], expected)
        for candidate in signatures
    )


class OpaqueFixtureStore(Store):
    """Redact only the serialized event, retaining the original stored request."""

    async def insert_message(self, message: dict[str, Any]) -> dict[str, Any]:
        stored = dict(message)
        event = json.loads(stored["event"], parse_constant=_reject_constant)
        data = event["data"]
        if not isinstance(data, dict):
            raise ValueError("fixture event data must be an object")
        data["text"] = GENERIC_EVENT_TEXT
        data["metadata"] = {}
        stored["event"] = _json_bytes(event).decode("utf-8")
        return await super().insert_message(stored)


class FixtureEngine:
    """One disposable engine, callback verifier, and bounded in-flight request map."""

    def __init__(self, database: Path):
        self.owner_id = "dbond-fixture-owner"
        self.owner_key = secrets.token_urlsafe(32)
        self.api_key = secrets.token_urlsafe(32)
        self.webhook_secret = "whsec_" + base64.b64encode(secrets.token_bytes(32)).decode(
            "ascii"
        )
        self.loop = asyncio.get_running_loop()
        self.waiters: dict[tuple[str, str], asyncio.Future[dict[str, Any]]] = {}
        self.original_inputs: dict[tuple[str, str], dict[str, Any]] = {}
        self.completed: OrderedDict[
            tuple[str, str], tuple[str, dict[str, Any]]
        ] = OrderedDict()
        self.submit_lock = asyncio.Lock()
        self.subscription_id: str | None = None
        self.challenge_signed = False
        settings = Settings(
            public_base_url="http://127.0.0.1",
            owner_user_id=self.owner_id,
            owner_key=self.owner_key,
            api_key=self.api_key,
            storage_key=Fernet.generate_key().decode("ascii"),
            db_path=database,
            durable_inbox=False,
            worker_interval_seconds=0.1,
            retry_base_seconds=0.1,
            max_delivery_attempts=1,
            reply_timeout_seconds=30,
        )
        self.bridge = DotsBridge(settings, webhook_post=self._injected_callback)
        fixture_store = OpaqueFixtureStore(
            settings.db_path,
            settings.storage_key,
            durable_inbox=settings.durable_inbox,
        )
        fixture_store.native_channels = self.bridge.store.native_channels
        self.bridge.store = fixture_store

    async def start(self) -> None:
        await self.bridge.initialize()
        subscription = await self.bridge.subscribe(
            {
                "name": EVENT_NAME,
                "arguments": {"channel": "fixture"},
                "delivery": {
                    "mode": "webhook",
                    "url": CALLBACK_URL,
                    "secret": self.webhook_secret,
                },
            },
            owner=self.owner_id,
        )
        self.subscription_id = subscription["id"]
        await self.bridge.start()

    async def close(self) -> None:
        await self.bridge.close()

    async def _injected_callback(
        self, url: str, body: bytes, headers: dict[str, str]
    ) -> httpx.Response:
        # This transport is injected directly; it never opens a socket or follows
        # the dummy .invalid URL stored in the disposable subscription.
        if url != CALLBACK_URL or not _verify_signature(body, headers, self.webhook_secret):
            return httpx.Response(401)
        try:
            event = json.loads(body, parse_constant=_reject_constant)
        except (UnicodeError, ValueError, TypeError):
            return httpx.Response(400)
        if event.get("type") == "verification":
            self.challenge_signed = True
            return httpx.Response(200, json={"challenge": event.get("challenge")})

        lowered = {key.lower(): value for key, value in headers.items()}
        if lowered.get("x-mcp-subscription-id") != self.subscription_id:
            return httpx.Response(401)
        if event.get("name") != EVENT_NAME or not isinstance(event.get("data"), dict):
            return httpx.Response(400)
        data = event["data"]
        if set(data) != {"request_id", "text", "channel", "metadata"}:
            return httpx.Response(400)
        if data.get("channel") != "fixture" or data.get("metadata") != {}:
            return httpx.Response(400)
        if data.get("text") != GENERIC_EVENT_TEXT:
            return httpx.Response(400)
        request_id = data.get("request_id")
        if not isinstance(request_id, str):
            return httpx.Response(409)

        try:
            acknowledgement = await self.bridge.acknowledge_request(request_id)
            if not acknowledgement.get("started_at"):
                return httpx.Response(409)
            original_metadata = acknowledgement.get("metadata")
            identity = (
                original_metadata.get("dbond_fixture")
                if isinstance(original_metadata, dict)
                else None
            )
            if not isinstance(identity, dict):
                return httpx.Response(409)
            service, source_id = identity.get("service"), identity.get("id")
            if not isinstance(service, str) or not isinstance(source_id, str):
                return httpx.Response(409)
            key = (service, source_id)
            waiter = self.waiters.get(key)
            expected = self.original_inputs.get(key)
            if waiter is None or expected is None:
                return httpx.Response(409)
            assert set(data) == {"request_id", "text", "channel", "metadata"}
            assert data["metadata"] == {}
            assert data["text"] == GENERIC_EVENT_TEXT
            assert expected["payload_text"] not in data["text"]
            assert acknowledgement.get("text") == expected["payload_text"]
            assert original_metadata == expected["metadata"]
            reply_text = f"fixture:{source_id}"
            reply = await self.bridge.reply_to_app(request_id, reply_text)
            if reply.get("reply") != reply_text:
                return httpx.Response(500)
        except BridgeError:
            return httpx.Response(409)

        if not waiter.done():
            waiter.set_result(
                {
                    "reply": reply_text,
                    "request_id": request_id,
                    "signed": bool(self.challenge_signed),
                    "started": True,
                    "replied": True,
                    "opaque": True,
                }
            )
        return httpx.Response(202)

    async def submit(self, document: dict[str, Any]) -> dict[str, Any]:
        service, source_id = document.get("service"), document.get("id")
        if not isinstance(service, str) or not 1 <= len(service) <= 128:
            raise ValueError("service must be a nonempty string of at most 128 characters")
        if not isinstance(source_id, str) or not 1 <= len(source_id) <= 128:
            raise ValueError("id must be a nonempty string of at most 128 characters")
        if "payload" not in document:
            raise ValueError("payload is required")

        payload_text = _json_bytes(document["payload"]).decode("utf-8")
        fingerprint = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
        key = (service, source_id)
        original_metadata = {"dbond_fixture": {"service": service, "id": source_id}}
        async with self.submit_lock:
            cached = self.completed.get(key)
            if cached:
                if cached[0] != fingerprint:
                    raise BridgeError(
                        "idempotency_conflict", "Fixture ID was reused with a different payload.", 409
                    )
                self.completed.move_to_end(key)
                return cached[1]

            waiter = self.loop.create_future()
            self.waiters[key] = waiter
            self.original_inputs[key] = {
                "payload_text": payload_text,
                "metadata": original_metadata,
            }
            idempotency_key = "dbond-fixture-" + hashlib.sha256(
                (service + "\0" + source_id).encode("utf-8")
            ).hexdigest()[:48]
            try:
                await self.bridge.send_message(
                    payload_text,
                    channel="fixture",
                    metadata=original_metadata,
                    idempotency_key=idempotency_key,
                )
                result = await asyncio.wait_for(waiter, timeout=OPERATION_TIMEOUT)
                self.completed[key] = (fingerprint, result)
                self.completed.move_to_end(key)
                while len(self.completed) > 128:
                    self.completed.popitem(last=False)
                return result
            finally:
                self.waiters.pop(key, None)
                self.original_inputs.pop(key, None)
                if not waiter.done():
                    waiter.cancel()


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def make_handler(engine: FixtureEngine):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _respond(self, status: int, value: dict[str, Any]) -> None:
            body = _json_bytes(value)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            try:
                remote = ipaddress.ip_address(self.client_address[0])
            except ValueError:
                self._respond(403, {"error": "loopback_only"})
                return
            if not remote.is_loopback:
                self._respond(403, {"error": "loopback_only"})
                return
            if self.path != "/fixture":
                self._respond(404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
                if not 1 <= length <= MAX_BODY:
                    raise ValueError
                document = json.loads(
                    self.rfile.read(length), parse_constant=_reject_constant
                )
                if not isinstance(document, dict) or set(document) != {
                    "service",
                    "id",
                    "payload",
                }:
                    raise ValueError
            except (UnicodeError, ValueError, TypeError):
                self._respond(400, {"error": "invalid_request"})
                return

            future = asyncio.run_coroutine_threadsafe(engine.submit(document), engine.loop)
            try:
                result = future.result(timeout=OPERATION_TIMEOUT + 3.0)
            except TimeoutError:
                future.cancel()
                self._respond(504, {"error": "fixture_timeout"})
            except BridgeError as exc:
                self._respond(exc.status_code, {"error": exc.code})
            except (ValueError, TypeError):
                self._respond(400, {"error": "invalid_request"})
            except Exception:
                self._respond(502, {"error": "fixture_failed"})
            else:
                self._respond(200, result)

    return Handler


def main() -> int:
    temporary = tempfile.TemporaryDirectory(prefix="dbond-fixture-")
    database = Path(temporary.name) / "bridge.sqlite3"
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, name="dbond-fixture-engine", daemon=True)
    loop_thread.start()
    engine: FixtureEngine
    try:
        startup = asyncio.run_coroutine_threadsafe(_make_engine(database), loop)
        engine = startup.result(timeout=8.0)
        server = FixtureServer(("127.0.0.1", 0), make_handler(engine))
    except Exception:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=3.0)
        loop.close()
        temporary.cleanup()
        raise

    server_thread = threading.Thread(
        target=server.serve_forever, name="dbond-fixture-http", daemon=True
    )
    server_thread.start()
    print(server.server_address[1], flush=True)
    stopped = threading.Event()

    def stop(_signum: int, _frame: Any) -> None:
        stopped.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        stopped.wait()
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=3.0)
        try:
            asyncio.run_coroutine_threadsafe(engine.close(), loop).result(timeout=5.0)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=3.0)
        loop.close()
        temporary.cleanup()
    return 0


async def _make_engine(database: Path) -> FixtureEngine:
    engine = FixtureEngine(database)
    await engine.start()
    return engine


if __name__ == "__main__":
    sys.exit(main())
