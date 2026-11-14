"""Validated macOS Claude peer inbox using host-inherited child capabilities.

The native Mac inbox authenticates local peers using OS credentials. Its user
wire frame is queued as external peer metadata, never as genuine human input.
The write socket has no processing ACK: submitted_unknown is deliberately not
host acceptance. Native held/refused receipts require a separate return inbox;
this adapter does not register or impersonate a Claude return session.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import struct
import subprocess
import sys
import uuid

REGISTRY_DIR = Path.home() / ".claude/sessions"
MAX_FRAME_BYTES = 1024 * 1024
_UUID = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


class InboxUnavailable(ValueError):
    """No safe live target; caller may await a registry change."""


@dataclass(frozen=True, repr=False)
class HostContext:
    """Host-issued child capability; memory only, never serialize or log it."""
    session_id: str
    pid: int
    proc_start: str
    socket_path: str
    _token: str = field(repr=False)
    _host_identity: tuple = field(repr=False)

    def __repr__(self):
        return "HostContext(<private inherited capability>)"


def validate_host_context(target: dict) -> HostContext:
    """Accept only a genuine current child of this exact live host.

    Bootstrap must finish before a parent detaches/exits. The resulting opaque
    capability may remain in this child's memory after reparenting; it does not
    permit replay to a restarted host and never upgrades broker text to consent.
    """
    fresh = discover_session(target["session_id"], Path(target["registry_dir"]))
    if not same_host(fresh, target):
        raise InboxUnavailable("host_changed")
    inherited_socket = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET", "")
    token = os.environ.get("CLAUDE_CODE_MESSAGING_TOKEN", "")
    if (not inherited_socket or str(Path(inherited_socket).resolve()) != target["socket_path"]
            or not re.fullmatch(r"[0-9a-fA-F]{32,256}", token)):
        raise InboxUnavailable("no_inherited_host_capability")
    pid = os.getpid()
    for _ in range(64):
        if pid <= 1:
            break
        proc = _process(pid)
        if proc["uid"] != os.getuid():
            break
        if pid == target["pid"]:
            if proc["proc_start"] != target["proc_start"]:
                break
            return HostContext(target["session_id"], target["pid"],
                               target["proc_start"], target["socket_path"], token,
                               _host_identity(target))
        pid = proc["ppid"]
    raise InboxUnavailable("not_an_own_host_child")


def _identity(st):
    return [st.st_dev, st.st_ino]


def _host_identity(target):
    """Stable host lifetime, excluding the atomically rewritten registry row."""
    names = ("session_id", "pid", "proc_start", "ppid", "parent_start",
             "socket_path", "registry_identity", "socket_identity",
             "socket_parent_identity", "executable_identity")
    return tuple(tuple(target[key]) if isinstance(target[key], list) else target[key]
                 for key in names)


def same_host(left: dict, right: dict) -> bool:
    """A fresh securely read registry row may have a different file inode."""
    try:
        return _host_identity(left) == _host_identity(right)
    except (KeyError, TypeError):
        return False


def _check_path(path, kind, private=True):
    st = path.lstat()
    valid = {"dir": stat.S_ISDIR, "file": stat.S_ISREG,
             "socket": stat.S_ISSOCK}[kind](st.st_mode)
    if not valid or st.st_uid != os.getuid() or st.st_mode & (0o077 if private else 0o022):
        raise InboxUnavailable("unsafe_" + kind)
    return st


def _process(pid):
    env = dict(os.environ, LC_ALL="C", TZ="UTC")
    raw = subprocess.check_output(
        ["/bin/ps", "-p", str(pid), "-o", "pid=,ppid=,uid=,lstart=,comm="],
        text=True, stderr=subprocess.DEVNULL, timeout=2, env=env).strip().split(None, 8)
    if len(raw) != 9:
        raise InboxUnavailable("process_unavailable")
    return {"pid": int(raw[0]), "ppid": int(raw[1]), "uid": int(raw[2]),
            "proc_start": " ".join(raw[3:8]), "executable": raw[8]}


def _channel_admitted(pid):
    raw = subprocess.check_output(["/bin/ps", "-p", str(pid), "-o", "command="],
                                  text=True, stderr=subprocess.DEVNULL, timeout=2)
    argv = shlex.split(raw)
    for i, value in enumerate(argv):
        for flag in ("--channels", "--dangerously-load-development-channels"):
            if value == flag:
                values = []
                for following in argv[i + 1:]:
                    if following.startswith("--"):
                        break
                    values.append(following)
            elif value.startswith(flag + "="):
                values = [value.split("=", 1)[1]]
            else:
                continue
            if any(re.search(r"(?:^|[,:])(?:plugin:)?d-bond(?:@[^, ]+)?(?:$|[, ])", v)
                   for v in values):
                return True
    return False


def discover_session(session_id: str, registry_dir: Path | None = None) -> dict:
    """Resolve exactly one live UUID without title matching or socket guessing."""
    if sys.platform != "darwin":
        raise InboxUnavailable("macos_only")
    if not isinstance(session_id, str) or not _UUID.fullmatch(session_id):
        raise InboxUnavailable("invalid_session_id")
    session_id = session_id.lower()
    root = Path(registry_dir) if registry_dir is not None else REGISTRY_DIR
    matches = []
    try:
        root_stat = _check_path(root, "dir")
        for record in root.glob("[0-9]*.json"):
            if not re.fullmatch(r"[1-9][0-9]*\.json", record.name):
                continue
            try:
                before = _check_path(record, "file", private=False)
                if before.st_size > 65536:
                    continue
                fd = os.open(record, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd) as src:
                    opened = os.fstat(src.fileno())
                    row = json.load(src)
                if _identity(before) != _identity(opened):
                    continue
            except (OSError, ValueError, TypeError):
                continue
            if not isinstance(row, dict) or str(row.get("sessionId", "")).lower() != session_id:
                continue
            pid = row.get("pid")
            if type(pid) is not int or pid <= 1 or record.stem != str(pid):
                raise InboxUnavailable("invalid_pid")
            proc = _process(pid)
            if proc["uid"] != os.getuid() or proc["proc_start"].split() != str(row.get("procStart", "")).split():
                raise InboxUnavailable("stale_or_foreign_process")
            executable = Path(proc["executable"])
            if executable.name != "claude":
                raise InboxUnavailable("not_claude")
            binary = executable.stat()
            if not stat.S_ISREG(binary.st_mode) or binary.st_uid not in (0, os.getuid()) or binary.st_mode & 0o022:
                raise InboxUnavailable("unsafe_executable")
            parent = _process(proc["ppid"])
            if parent["uid"] not in (0, os.getuid()):
                raise InboxUnavailable("foreign_parent")
            if _identity(_check_path(record, "file", private=False)) != _identity(opened):
                raise InboxUnavailable("record_replaced")
            # Only a verified live host may be classified as unsupported. A
            # stale/foreign/unsafe record must never enable a weaker fallback.
            if row.get("pidDomain") != "darwin" or row.get("peerProtocol") != 1:
                raise InboxUnavailable("unsupported_peer_domain_or_protocol")
            socket_path = row.get("messagingSocketPath")
            if socket_path is None or socket_path == "":
                raise InboxUnavailable("unsupported_missing_inbox")
            if not isinstance(socket_path, str):
                raise InboxUnavailable("invalid_socket_path")
            sock = Path(socket_path)
            if not sock.is_absolute() or sock.name != f"{pid}.sock":
                raise InboxUnavailable("invalid_socket_path")
            # /tmp is an OS alias on macOS; resolve its parent once, then retain
            # device/inode identities to reject replacement between checks.
            parent_path = sock.parent.resolve(strict=True)
            parent_stat = _check_path(parent_path, "dir")
            canonical = parent_path / sock.name
            socket_stat = _check_path(canonical, "socket")
            if _identity(_check_path(record, "file", private=False)) != _identity(opened):
                raise InboxUnavailable("record_replaced")
            matches.append({"session_id": session_id, "pid": pid,
                "proc_start": proc["proc_start"], "ppid": proc["ppid"],
                "parent_start": parent["proc_start"], "socket_path": str(canonical),
                "registry_dir": str(root), "record_path": str(record),
                "registry_identity": _identity(root_stat), "record_identity": _identity(opened),
                "socket_identity": _identity(socket_stat), "socket_parent_identity": _identity(parent_stat),
                "executable_identity": _identity(binary), "native_channel": _channel_admitted(pid)})
    except InboxUnavailable:
        raise
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise InboxUnavailable("host_unavailable") from exc
    if len(matches) != 1:
        raise InboxUnavailable("no_unique_live_session")
    return matches[0]


def _peer_credentials(conn):
    """Darwin getpeereid + LOCAL_PEERPID; fail closed when unsupported."""
    libc = ctypes.CDLL(None, use_errno=True)
    uid, gid = ctypes.c_uint(), ctypes.c_uint()
    if libc.getpeereid(conn.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
        raise InboxUnavailable("peer_uid_unavailable")
    pid = struct.unpack("i", conn.getsockopt(0, 2, struct.calcsize("i")))[0]
    return uid.value, pid


def message_id(session_id: str, event_id: str) -> str:
    if not isinstance(event_id, str) or not event_id or len(event_id) > 1024:
        raise ValueError("invalid_event_id")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "d-bond:claude:" + session_id + ":" + event_id))


def deliver(target: dict, text: str, event_id: str, sender_name: str = "D-Bond",
            timeout: float = 2.0, host_context: HostContext | None = None) -> dict:
    """One write attempt. Never retry an indeterminate write or claim receipt."""
    mid = message_id(target["session_id"], event_id)
    result = {"msg_id": mid, "session_id": target["session_id"],
              "state": "offline", "basis": "pre_write_failure", "write_attempted": False}
    if not isinstance(text, str) or not text.strip() or not isinstance(sender_name, str) or not sender_name.strip():
        return dict(result, state="rejected", reason="invalid_message")
    frame = {"type": "user", "session_id": target["session_id"], "uuid": mid,
             "msg_id": mid, "from": "D-Bond", "priority": "next",
             "message": {"role": "user", "content": f"D-Bond peer message from {sender_name}:\n{text}"}}
    wire = (json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(wire) > MAX_FRAME_BYTES:
        return dict(result, state="rejected", reason="message_too_large")
    if host_context is not None:
        if not isinstance(host_context, HostContext) or (
                host_context.session_id, host_context.pid, host_context.proc_start,
                host_context.socket_path) != (
                target["session_id"], target["pid"], target["proc_start"], target["socket_path"]) or (
                host_context._host_identity != _host_identity(target)):
            return dict(result, state="rejected", reason="wrong_host_context")
        # Only the inherited capability is used; never read a peer key file or
        # another process's environment. The external D-Bond sender stays in
        # the message; this transport capability grants no human permission.
        wire = (json.dumps({"type": "auth", "token": host_context._token},
                           separators=(",", ":")) + "\n").encode("ascii") + wire
    try:
        current = discover_session(target["session_id"], Path(target["registry_dir"]))
        if not same_host(current, target):
            return dict(result, reason="host_changed")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout)
            conn.connect(target["socket_path"])
            uid, pid = _peer_credentials(conn)
            if uid != os.getuid() or pid != target["pid"]:
                return dict(result, state="rejected", reason="wrong_endpoint")
            if not same_host(discover_session(target["session_id"], Path(target["registry_dir"])), target):
                return dict(result, reason="host_changed")
            # A successful socket write is not a native processing receipt. Even
            # an exception here may follow a partial write: quarantine either.
            result.update(state="submitted_unknown", basis="socket_write_only", write_attempted=True)
            conn.sendall(wire)
            conn.shutdown(socket.SHUT_WR)
        return result
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return dict(result, reason=type(exc).__name__)
