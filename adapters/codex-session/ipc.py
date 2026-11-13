"""Read-only owner discovery through Codex Desktop IPC.

Protocol shape was identified from yieldspace/agmsg-codex-relay's
``codex_app_ipc.py`` (MIT, Copyright 2026 sizumita):
https://github.com/yieldspace/agmsg-codex-relay

This is a small independent implementation. It identifies itself truthfully as
``D-Bond`` and does not impersonate a first-party client. The IPC is private and
version-sensitive. Delivery uses the exact Desktop owner and never impersonates a client.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import struct
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


SOCKET_PATH = Path.home() / ".codex" / "ipc" / "ipc.sock"
MAX_FRAME_BYTES = 1024 * 1024
DEFAULT_TIMEOUT = 20.0
_THREAD_OWNER_DISCOVERY_VERSION = 1


class CodexWakeError(RuntimeError):
    """A local IPC validation, protocol, or transport failure."""


def _validate_socket(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CodexWakeError(f"Codex IPC socket unavailable at {path}: {exc}") from exc
    if not stat.S_ISSOCK(metadata.st_mode):
        raise CodexWakeError(f"Codex IPC path is not a socket: {path}")
    if metadata.st_uid != os.getuid():
        raise CodexWakeError("Codex IPC socket is not owned by the current user")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise CodexWakeError("Codex IPC socket grants group or other access")
    return metadata


class IPCRejected(CodexWakeError):
    """An explicit router or owner rejection, distinct from transport uncertainty."""

    def __init__(self, method: str, error: Any):
        self.method = method
        self.error = str(error)
        super().__init__(f"Codex IPC {method} rejected: {self.error}")


class DeliveryUncertain(CodexWakeError):
    """Delivery may have reached the owner; reconcile before any retry."""


class _Client:
    def __init__(self, path: Path, timeout: float):
        if not 0 < timeout <= 30:
            raise ValueError("timeout must be greater than 0 and at most 30 seconds")
        self.path = path
        self.timeout = timeout
        self._deadline = 0.0
        self.client_id: str | None = None
        metadata = _validate_socket(path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(str(path))
            connected = path.lstat()
            if (connected.st_dev, connected.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise CodexWakeError("Codex IPC socket changed during connection")
        except Exception:
            self.sock.close()
            raise

    def close(self) -> None:
        self.sock.close()

    def _send_frame(self, value: dict[str, Any]) -> None:
        payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if not payload or len(payload) > MAX_FRAME_BYTES:
            raise CodexWakeError("Codex IPC request frame exceeds the 1 MiB limit")
        try:
            self.sock.settimeout(max(0.001, self._deadline - time.monotonic()))
            self.sock.sendall(struct.pack("<I", len(payload)) + payload)
        except socket.timeout as exc:
            raise CodexWakeError("Codex IPC request timed out while writing") from exc
        except OSError as exc:
            raise CodexWakeError(
                f"Codex IPC write failed; request delivery may be ambiguous: {exc}"
            ) from exc

    def _read_exact(self, size: int) -> bytes:
        data = bytearray()
        try:
            while len(data) < size:
                remaining = self._deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout()
                self.sock.settimeout(remaining)
                chunk = self.sock.recv(size - len(data))
                if not chunk:
                    raise CodexWakeError("Codex IPC closed before completing its response")
                data.extend(chunk)
        except socket.timeout as exc:
            raise CodexWakeError("Codex IPC response timed out") from exc
        except OSError as exc:
            raise CodexWakeError(f"Codex IPC read failed: {exc}") from exc
        return bytes(data)

    def _read_frame(self) -> dict[str, Any]:
        size = struct.unpack("<I", self._read_exact(4))[0]
        if size < 1 or size > MAX_FRAME_BYTES:
            raise CodexWakeError(f"Invalid Codex IPC frame length: {size}")
        try:
            value = json.loads(self._read_exact(size))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CodexWakeError("Codex IPC returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise CodexWakeError("Codex IPC response must be a JSON object")
        return value

    def request(
        self,
        method: str,
        params: dict[str, Any],
        version: int,
        *,
        target_client_id: str | None = None,
        host_id: str | None = None,
    ) -> dict[str, Any]:
        self._deadline = time.monotonic() + self.timeout
        request_id = str(uuid.uuid4())
        request = {
            "type": "request",
            "requestId": request_id,
            "sourceClientId": self.client_id or "initializing-client",
            "version": version,
            "method": method,
            "params": params,
            "timeoutMs": int(self.timeout * 1000),
        }
        if target_client_id is not None:
            request["targetClientId"] = target_client_id
        if host_id is not None:
            request["hostId"] = host_id
        self._send_frame(request)
        while True:
            response = self._read_frame()
            if response.get("type") == "client-discovery-request":
                discovery_id = response.get("requestId")
                if not isinstance(discovery_id, str) or not discovery_id:
                    raise CodexWakeError("Codex IPC discovery request has no requestId")
                self._send_frame({
                    "type": "client-discovery-response",
                    "requestId": discovery_id,
                    "response": {"canHandle": False},
                })
                continue
            # The router multiplexes owner notifications and stream broadcasts on
            # this socket. They are unrelated to this request and must not derail it.
            if response.get("type") != "response" or response.get("requestId") != request_id:
                continue
            if response.get("resultType") != "success":
                raise IPCRejected(method, response.get("error", "unknown error"))
            return response

    def initialize(self) -> str:
        response = self.request("initialize", {"clientType": "D-Bond"}, version=0)
        result = response.get("result")
        client_id = result.get("clientId") if isinstance(result, dict) else None
        if not isinstance(client_id, str) or not client_id:
            raise CodexWakeError("Codex IPC initialize returned no clientId")
        self.client_id = client_id
        return client_id


class OwnerUnavailable(CodexWakeError):
    """Exact thread has no safely resolved owner; no turn was submitted."""


def handshake(*, timeout: float = DEFAULT_TIMEOUT, socket_path: Path = SOCKET_PATH) -> dict[str, str]:
    """Read-only initialize handshake."""
    client = _Client(socket_path, timeout)
    try:
        return {"status": "initialized", "client_id": client.initialize()}
    finally:
        client.close()


def _discover_host(thread_id: str, host_id: str, timeout: float,
                   socket_path: Path) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    client = _Client(socket_path, timeout)
    try:
        client.initialize()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CodexWakeError("Codex owner discovery timed out")
        client.timeout = remaining
        return client.request("thread-owner-discovery",
            {"hostId": host_id, "conversationId": thread_id},
            version=_THREAD_OWNER_DISCOVERY_VERSION)
    finally:
        client.close()


def discover_owner(thread_id: str, *, host_id: str | None = None,
                   timeout: float = DEFAULT_TIMEOUT,
                   socket_path: Path = SOCKET_PATH) -> dict[str, str | bool]:
    """Find the exact UUID on local/durable, or recheck a known canonical host.

    Unknown hosts are queried concurrently within one bounded discovery window.
    No turn is submitted. A timeout or missing owner is safe to retry later.
    Once discovered, persist host_id and pass it back to avoid cold-host waits.
    """
    thread_id = str(uuid.UUID(thread_id))
    if host_id is not None and host_id not in ("local", "durable"):
        raise ValueError("Unsupported Codex host_id")
    if not 0 < timeout <= 30:
        raise ValueError("timeout must be greater than 0 and at most 30 seconds")
    hosts = (host_id,) if host_id is not None else ("local", "durable")
    responses = []
    failures = []
    with ThreadPoolExecutor(max_workers=len(hosts)) as executor:
        pending = [(host, executor.submit(_discover_host, thread_id, host,
                                         timeout, socket_path)) for host in hosts]
        for host, future in pending:
            try:
                response = future.result()
            except (CodexWakeError, OSError) as exc:
                failures.append(exc)
                continue
            client_id = response.get("handledByClientId")
            if not isinstance(client_id, str) or not client_id:
                raise CodexWakeError("Codex owner discovery returned no handledByClientId")
            responses.append((host, client_id, response.get("result")))
    if len(responses) > 1:
        raise OwnerUnavailable("Exact Codex thread has ambiguous local/durable owners")
    if not responses:
        raise OwnerUnavailable("Exact Codex thread has no available owner on "
                               + ", ".join(hosts)) from (failures[0] if failures else None)
    host, client_id, result = responses[0]
    if not isinstance(result, dict) or result.get("supportsUntrustedAppInput") is not True:
        raise CodexWakeError("Codex owner does not support untrusted app input")
    return {"thread_id": thread_id, "host_id": host,
            "handled_by_client_id": client_id, "supports_untrusted_app_input": True}


def deliver_to_owner(thread_id: str, prompt: str, owner: dict[str, Any], *,
                     timeout: float = DEFAULT_TIMEOUT,
                     socket_path: Path = SOCKET_PATH) -> dict[str, str]:
    """Steer the exact Desktop owner, or start after an explicit inactive rejection.

    No queue, standalone CLI resume, client impersonation, permission override,
    or cross-server fallback is allowed. A transport failure is not a retry cue.
    Acceptance confirms host submission, not model consumption or a reply.
    """
    thread_id = str(uuid.UUID(thread_id))
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Delivery requires nonempty message text")
    if (owner.get("thread_id") != thread_id or
            owner.get("supports_untrusted_app_input") is not True or
            not isinstance(owner.get("handled_by_client_id"), str) or
            not owner["handled_by_client_id"]):
        raise ValueError("Delivery requires the exact discovered Desktop owner")
    target = owner["handled_by_client_id"]
    # Legacy owner dictionaries predate durable routing and denote local only.
    host_id = owner.get("host_id", "local")
    if host_id not in ("local", "durable"):
        raise ValueError("Unsupported Codex owner host_id")
    route = {"host_id": host_id} if host_id != "local" else {}
    version_offset = int(host_id != "local")
    try:
        client = _Client(socket_path, timeout)
    except (OSError, CodexWakeError) as exc:
        raise OwnerUnavailable(str(exc)) from exc
    message_id = str(uuid.uuid4())
    inputs = [{"type": "text", "text": prompt, "text_elements": []}]
    restore = {"id": message_id, "text": prompt, "context": {}}
    dispatched = False
    try:
        client.initialize()
        # Revalidate on this connection; never redirect a stale owner silently.
        discovery = client.request("thread-owner-discovery",
            {"hostId": host_id, "conversationId": thread_id}, version=1,
            target_client_id=target)
        if (discovery.get("handledByClientId") != target or
                not isinstance(discovery.get("result"), dict) or
                discovery["result"].get("supportsUntrustedAppInput") is not True):
            raise OwnerUnavailable("Exact Codex owner changed or is unsupported")
        phase = "steered"
        dispatched = True
        try:
            response = client.request("thread-follower-steer-turn", {
                "conversationId": thread_id, "input": inputs,
                "restoreMessage": restore, "clientUserMessageId": message_id,
                "attachments": [],
            }, version=1 + version_offset, target_client_id=target, **route)
        except IPCRejected as exc:
            # This owner-side error precedes submission. All other errors retain
            # their uncertainty; in particular a timeout must never start a turn.
            if exc.error != (f"Cannot steer conversation {thread_id} because "
                             "its active turn already ended"):
                raise
            phase = "started"
            response = client.request("thread-follower-start-turn", {
                "conversationId": thread_id,
                "turnStart": {
                    "request": {"threadId": thread_id, "input": inputs,
                                "clientUserMessageId": message_id},
                    "context": {"inheritThreadSettings": True,
                                "restoreMessage": restore},
                },
            }, version=2 + version_offset, target_client_id=target, **route)
        result = response.get("result")
        result = result.get("result") if isinstance(result, dict) else None
        if phase == "started":
            # startTurn returns the raw TurnStartResponse; unlike steerTurn,
            # its exact turn receipt is nested in result.turn.id.
            turn = result.get("turn") if isinstance(result, dict) else None
            receipt = turn.get("id") if isinstance(turn, dict) else None
            try:
                if not isinstance(receipt, str) or str(uuid.UUID(receipt)) != receipt:
                    raise ValueError("Invalid canonical turn UUID")
            except (ValueError, AttributeError) as exc:
                raise DeliveryUncertain("Codex start response has no exact turn UUID") from exc
        else:
            receipt = result.get("turnId") if isinstance(result, dict) else result
            if not isinstance(receipt, str) or not receipt:
                raise DeliveryUncertain("Codex delivery response has no turn receipt")
        return {"phase": phase, "receipt": receipt, "thread_id": thread_id, "host_id": host_id,
                "client_user_message_id": message_id}
    except OwnerUnavailable:
        raise
    except IPCRejected as exc:
        if exc.error in ("no-client-found", "request-version-mismatch"):
            raise OwnerUnavailable(str(exc)) from exc
        raise DeliveryUncertain(str(exc)) from exc
    except CodexWakeError as exc:
        if not dispatched:
            raise OwnerUnavailable(str(exc)) from exc
        raise DeliveryUncertain(str(exc)) from exc
    finally:
        client.close()
