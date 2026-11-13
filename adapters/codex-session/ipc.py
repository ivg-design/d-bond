"""Read-only owner discovery through Codex Desktop IPC.

Protocol shape was identified from yieldspace/agmsg-codex-relay's
``codex_app_ipc.py`` (MIT, Copyright 2026 sizumita):
https://github.com/yieldspace/agmsg-codex-relay

This is a small independent implementation. It identifies itself truthfully as
``D-Bond`` and does not impersonate a first-party client. The IPC is private and
version-sensitive; this module never submits a turn.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import struct
import time
import uuid
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
                raise CodexWakeError(
                    f"Codex IPC {method} rejected: {response.get('error', 'unknown error')}"
                )
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
    """Exact thread has no local owner; no start-turn was submitted."""


def handshake(*, timeout: float = DEFAULT_TIMEOUT, socket_path: Path = SOCKET_PATH) -> dict[str, str]:
    """Read-only initialize handshake."""
    client = _Client(socket_path, timeout)
    try:
        return {"status": "initialized", "client_id": client.initialize()}
    finally:
        client.close()


def discover_owner(thread_id: str, *, timeout: float = DEFAULT_TIMEOUT,
                   socket_path: Path = SOCKET_PATH) -> dict[str, str | bool]:
    """Read-only exact owner discovery. Missing owner is safe to retry later."""
    thread_id = str(uuid.UUID(thread_id))
    client = _Client(socket_path, timeout)
    try:
        client.initialize()
        try:
            response = client.request("thread-owner-discovery",
                {"hostId": "local", "conversationId": thread_id},
                version=_THREAD_OWNER_DISCOVERY_VERSION)
        except CodexWakeError as exc:
            if "no-client-found" in str(exc):
                raise OwnerUnavailable("Exact Codex thread has no local owner") from exc
            raise
        client_id = response.get("handledByClientId")
        result = response.get("result")
        supports_untrusted = result.get("supportsUntrustedAppInput") if isinstance(result, dict) else None
        if not isinstance(client_id, str) or not client_id:
            raise CodexWakeError("Codex owner discovery returned no handledByClientId")
        if supports_untrusted is not True:
            raise CodexWakeError("Codex owner does not support untrusted app input")
        return {"thread_id": thread_id, "handled_by_client_id": client_id,
                "supports_untrusted_app_input": True}
    finally:
        client.close()
