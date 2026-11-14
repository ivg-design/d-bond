"""D-Bond-owned single-route durable inbox layered on unmodified MIT upstream."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from uuid import uuid4

import httpx
from dots_bridge.app import create_app as upstream_app
from dots_bridge.bridge import DotsBridge
from dots_bridge.errors import BridgeError
from dots_bridge.models import EVENT_NAME, MessageInput, iso8601
from dots_bridge.store import Store, public_message
from dots_bridge.webhooks import signed_body
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import require_scopes
from starlette.responses import JSONResponse
from starlette.routing import Route

CHANNEL = "d-bond"


class InboxStore(Store):
    async def initialize(self):
        await super().initialize()
        if getattr(self, "_extended", False):
            return
        # Separate versioned extension: upstream files and schema marker stay intact.
        def extend(db):
            columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
            if "started_at" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN started_at REAL")
            db.execute("CREATE TABLE IF NOT EXISTS dbond_host (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO dbond_host VALUES ('schema','1')")
        async with self._lock:
            if not getattr(self, "_extended", False):
                await self.run(extend)
                self._extended = True

    @staticmethod
    def _require_active_request(db, row):
        # Callback expiry is transport loss; an explicitly revoked/cancelled request stays closed.
        if row["reply_cancelled"] or row["superseded_by"] is not None:
            raise BridgeError("message_inactive", "This request is no longer active.", 409)
        if row["expires_at"] <= time.time():
            raise BridgeError("reply_expired", "The reply deadline has passed.", 410)

    async def insert_message(self, message):
        def insert(db):
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM messages WHERE idempotency_key=?", (message["idempotency_key"],)).fetchone()
            if prior:
                return dict(prior)
            columns = ",".join(message)
            db.execute(f"INSERT INTO messages ({columns}) VALUES ({','.join('?' for _ in message)})", tuple(message.values()))
            return dict(db.execute("SELECT * FROM messages WHERE request_id=?", (message["request_id"],)).fetchone())
        return await self.run(insert)

    async def save_subscription(self, sub):
        encrypted = self.cipher.encrypt(json.dumps(sub["destination"]).encode())
        def save(db):
            db.execute("BEGIN IMMEDIATE")
            # One owner, one channel, one active callback. A replacement is a transport rebind.
            db.execute("DELETE FROM subscriptions WHERE owner=? AND channel=? AND id<>?", (sub["owner"], CHANNEL, sub["id"]))
            db.execute("INSERT INTO subscriptions VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                       "destination=excluded.destination,expires_at=excluded.expires_at,verified_at=excluded.verified_at",
                       (sub["id"], sub["owner"], CHANNEL, encrypted, sub["expires_at"], sub["verified_at"]))
        await self.run(save)

    async def revoke_subscriptions(self, owner):
        # Also close work queued before a callback ever existed.
        def revoke(db):
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE messages SET reply_cancelled=1,delivery_status='cancelled',error='authorization_revoked' WHERE reply IS NULL")
            db.execute("DELETE FROM subscriptions WHERE owner=?", (owner,))
        await self.run(revoke)


class DotInbox(DotsBridge):
    def __init__(self, settings, **kwargs):
        super().__init__(settings, **kwargs)
        self.store = InboxStore(settings.db_path, settings.storage_key)
        self._grant_lock = asyncio.Lock()

    async def subscribe(self, params, owner):
        # Serialize callback verification with explicit unsubscribe/revocation so a
        # slow successful verification cannot revive the grant just revoked.
        async with self._grant_lock:
            result = await super().subscribe(params, owner)
            self._wake.set()
            return result

    async def unsubscribe(self, params, owner):
        async with self._grant_lock:
            return await super().unsubscribe(params, owner)

    async def revoke_subscriptions(self):
        async with self._grant_lock:
            return await super().revoke_subscriptions()

    async def list_events(self):
        rows = await super().list_events()
        rows[0]["inputSchema"]["properties"]["channel"] = {"type": "string", "const": CHANNEL, "default": CHANNEL}
        return rows

    def _subscription_args(self, params, owner):
        # A missing channel never silently selects another inbox.
        params = {**params, "arguments": params.get("arguments") or {"channel": CHANNEL}}
        result = super()._subscription_args(params, owner)
        if result[1] != CHANNEL:
            raise BridgeError("invalid_channel", "D-Bond supports only its d-bond channel.")
        return result

    async def submit(self, message: MessageInput):
        await self.initialize()
        if message.channel != CHANNEL or message.subscription_id is not None:
            raise BridgeError("invalid_channel", "D-Bond selects its single d-bond route.", 422)
        canonical = json.dumps(message.model_dump(exclude={"idempotency_key"}), ensure_ascii=False,
                               sort_keys=True, separators=(",", ":"), allow_nan=False)
        fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
        if message.idempotency_key:
            prior = await self.store.by_idempotency_key(message.idempotency_key)
            if prior:
                return self._check_idempotency(prior, fingerprint)
        now = time.time()
        request, event_id = "req_" + uuid4().hex, "evt_" + uuid4().hex
        event = {"eventId": event_id, "name": EVENT_NAME, "timestamp": iso8601(now), "cursor": None,
                 "data": {"request_id": request, "text": message.text, "channel": CHANNEL, "metadata": message.metadata}}
        body = json.dumps(event, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(body.encode()) > 262144:
            raise BridgeError("message_too_large", "Serialized event exceeds 256 KiB.", 413)
        subscriptions = await self.store.subscriptions(self.owner_user_id)
        row = await self.store.insert_message({"request_id": request, "event_id": event_id,
            "subscription_id": subscriptions[0]["id"] if len(subscriptions) == 1 else "",
            "channel": CHANNEL, "text": message.text, "metadata": json.dumps(message.metadata, ensure_ascii=False, allow_nan=False),
            "created_at": now, "expires_at": now + self.settings.reply_timeout_seconds, "next_attempt_at": now,
            "idempotency_key": message.idempotency_key, "fingerprint": fingerprint, "event": body})
        self._wake.set()
        return self._check_idempotency(row, fingerprint)

    async def acknowledge_request(self, request_id):
        await self.initialize()
        def acknowledge(db):
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM messages WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                raise BridgeError("message_not_found", "No matching message.", 404)
            self.store._require_active_request(db, row)
            already = row["started_at"] is not None
            db.execute("UPDATE messages SET started_at=COALESCE(started_at,?) WHERE request_id=?", (time.time(), request_id))
            saved = dict(db.execute("SELECT * FROM messages WHERE request_id=?", (request_id,)).fetchone())
            result = public_message(saved)
            result.update(already_started=already, started_at=iso8601(saved["started_at"]))
            return result
        return await self.store.run(acknowledge)

    async def get_message(self, request_id):
        result = await super().get_message(request_id)
        row = await self.store.message(request_id)
        result["started_at"] = iso8601(row["started_at"]) if row["started_at"] is not None else None
        return result

    async def cancel_request(self, request_id, *, superseded_by):
        await self.initialize()
        original = await self.store.message(request_id)
        replacement = await self.store.message(superseded_by)
        before, after = json.loads(original["metadata"]), json.loads(replacement["metadata"])
        if any(before.get(key) != after.get(key) for key in ("caller_id", "conversation_id")):
            raise BridgeError("invalid_cancellation", "A replacement must belong to the same paired conversation.", 422)
        return await super().cancel_request(request_id, superseded_by=superseded_by)

    async def send_to_app(self, text, *, idempotency_key, channel=CHANNEL, metadata=None):
        if channel != CHANNEL:
            raise BridgeError("invalid_channel", "D-Bond supports only its d-bond channel.", 422)
        # Addressed push must name this broker and an exact public service UUID.
        metadata = metadata or {}
        try:
            recipient = str(__import__("uuid").UUID(metadata.get("recipient_conversation_id", "")))
        except (ValueError, AttributeError):
            raise BridgeError("invalid_recipient", "An exact paired D-Bond recipient is required.", 422) from None
        if metadata.get("recipient_caller_id") != CHANNEL or recipient != metadata["recipient_conversation_id"]:
            raise BridgeError("invalid_recipient", "An exact D-Bond recipient is required.", 422)
        return await super().send_to_app(text, idempotency_key=idempotency_key, channel=CHANNEL, metadata=metadata)

    async def process_next(self):
        await self.initialize()
        subscriptions = await self.store.subscriptions(self.owner_user_id)
        if len(subscriptions) != 1:
            return False
        sub = subscriptions[0]
        # Single process is enforced by serve's flock. Lease persists across interruption.
        lease = secrets.token_hex(16)
        def claim(db):
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            row = db.execute("SELECT * FROM messages WHERE reply IS NULL AND started_at IS NULL "
                "AND reply_cancelled=0 AND expires_at>? AND (delivery_status='queued' AND next_attempt_at<=? "
                "OR delivery_status='delivering' AND lease_until<=?) ORDER BY created_at LIMIT 1", (now, now, now)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE messages SET delivery_status='delivering',attempts=attempts+1,lease_until=?,"
                       "subscription_id=?,error=? WHERE request_id=?", (now + 30, sub["id"], lease, row["request_id"]))
            return dict(db.execute("SELECT * FROM messages WHERE request_id=?", (row["request_id"],)).fetchone())
        row = await self.store.run(claim)
        if row is None:
            return False
        destination = self.store.destination(sub)
        body, headers = signed_body(sub["id"], destination["secret"], row["event_id"], json.loads(row["event"]),
            old_secret=destination.get("old_secret") if destination.get("old_secret_until", 0) > time.time() else None)
        code, error, state = None, None, "queued"
        try:
            response = await self.webhook_post(destination["url"], body, headers)
            code = response.status_code
            if response.is_success:
                state = "accepted"
            elif code == 410:
                # Transport callback died. Keep the durable request and fence against a fresh refresh.
                await self.store.run(lambda db: db.execute("DELETE FROM subscriptions WHERE id=? AND destination=? AND expires_at=?",
                    (sub["id"], sub["destination"], sub["expires_at"])))
                error = "callback_gone"
            elif code not in (408, 425, 429) and code < 500:
                state, error = "failed", "webhook_rejected"
        except (httpx.HTTPError, OSError, TimeoutError):
            error = "webhook_unavailable"
        except ValueError:
            state, error = "failed", "invalid_webhook_destination"
        delay = min(300, self.settings.retry_base_seconds * 2 ** min(30, row["attempts"] - 1))
        def finish(db):
            # Fence late results from expired leases and prevent resurrection after revocation.
            db.execute("UPDATE messages SET delivery_status=?,status_code=?,error=?,next_attempt_at=?,lease_until=NULL "
                "WHERE request_id=? AND delivery_status='delivering' AND error=? AND reply_cancelled=0",
                (state, code, error, time.time() + delay, row["request_id"], lease))
        await self.store.run(finish)
        return True


def create_app(settings, *, bridge=None, start_worker=True):
    bridge = bridge or DotInbox(settings)
    app = upstream_app(settings, bridge=bridge, start_worker=start_worker)
    mcp = app.state.mcp

    @mcp.tool(auth=require_scopes("messages:reply"), annotations={"idempotentHint": True, "readOnlyHint": False},
              meta={"securitySchemes": [{"type": "oauth2", "scopes": ["messages:reply"]}]})
    async def acknowledge_request(request_id: str):
        """Acknowledge this exact request before working; repeated ACKs preserve its start time."""
        try:
            return await bridge.acknowledge_request(request_id)
        except BridgeError as exc:
            raise ToolError(f"{exc.code}: {exc.message}") from None

    # Replace just the local compatibility/status routes; all MCP and OAuth stay upstream-owned.
    def local_authorize(request):
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), settings.api_key.encode()):
            raise BridgeError("unauthorized", "Application API authorization required.", 401)

    async def subscriptions(request):
        local_authorize(request)
        return JSONResponse({"subscriptions": await bridge.list_subscriptions(), "slack_channels": [], "native_channels": []})

    async def identity(request):
        local_authorize(request)
        active = await bridge.list_subscriptions()
        return JSONResponse({"host": "d-bond-dot-host/1", "store_id": settings.owner_user_id, "pid": os.getpid(),
                             "active_subscriptions": len(active), "subscribed": len(active) == 1})

    async def stop(request):
        local_authorize(request)
        callback = getattr(app.state, "shutdown_callback", None)
        if callback is None:
            return JSONResponse({"stopping": False}, status_code=409)
        callback()
        return JSONResponse({"stopping": True, "store_id": settings.owner_user_id})

    app.router.routes.insert(0, Route("/api/host-status", identity))
    app.router.routes.insert(0, Route("/api/host-stop", stop, methods=["POST"]))
    app.router.routes.insert(0, Route("/api/subscriptions", subscriptions))
    return app
