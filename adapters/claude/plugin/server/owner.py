"""Same-user owner-socket client for D-Bond configuration and trace.

This module never reads or accepts service bearer credentials. The server also
checks the Unix peer UID before serving owner operations.
"""
from __future__ import annotations

import json
import os
import plistlib
import socket
import stat
import subprocess
import time
from pathlib import Path

OWNER_SOCKET = Path.home() / "Library/Application Support/D-Bond Proof of Concept/pair.sock"
MAX_REPLY = 2 * 1024 * 1024


def request(payload: dict, path: Path = OWNER_SOCKET, timeout: float = 5) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Owner request must be an object")
    home = Path.home()
    try:
        parts = path.relative_to(home).parts
    except ValueError as exc:
        raise ValueError("Owner socket must be inside this user's home") from exc
    current = home
    for part in parts[:-1]:
        current /= part
        if current.is_symlink():
            raise ValueError("Owner socket path contains a symlink")
    directory = path.parent.stat()
    endpoint = path.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
            or directory.st_mode & 0o077 or not stat.S_ISSOCK(endpoint.st_mode)
            or endpoint.st_uid != os.getuid() or endpoint.st_mode & 0o077):
        raise ValueError("D-Bond owner socket has unsafe ownership or permissions")
    frame = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    if len(frame) > 256 * 1024:
        raise ValueError("Owner request is too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        deadline = time.monotonic() + timeout
        client.settimeout(timeout)
        client.connect(str(path))
        client.settimeout(max(0.001, deadline - time.monotonic()))
        client.sendall(frame)
        data = bytearray()
        while len(data) <= MAX_REPLY and not data.endswith(b"\n"):
            client.settimeout(max(0.001, deadline - time.monotonic()))
            chunk = client.recv(8192)
            if not chunk:
                break
            data.extend(chunk)
    if not data.endswith(b"\n") or len(data) > MAX_REPLY:
        raise ValueError("Incomplete or oversized D-Bond owner reply")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("Invalid D-Bond owner reply")
    if "error" in result:
        raise ValueError(str(result["error"]))
    return result


def local_status() -> dict:
    """Return a bounded, credential-free app and owner-API check on demand."""
    # A successful owner API response is direct evidence of a running app.
    # Never return the response: it can contain private connection details.
    try:
        reply = request({"op": "list"}, timeout=0.75)
        if isinstance(reply.get("services"), list):
            return {"installation": "installed", "service": "reachable"}
    except (OSError, ValueError, json.JSONDecodeError):
        pass

    home = Path.home()
    candidates = (home / "Applications/D-Bond.app", Path("/Applications/D-Bond.app"))
    for app in candidates:
        try:
            if app.is_symlink():
                continue
            with (app / "Contents/Info.plist").open("rb") as stream:
                info = plistlib.load(stream)
            if (info.get("CFBundleExecutable") == "DBond"
                    and info.get("CFBundleName") == "D-Bond"
                    and (app / "Contents/MacOS/DBond").is_file()):
                return {"installation": "installed", "service": "offline"}
        except (OSError, ValueError, TypeError, plistlib.InvalidFileException):
            continue

    # The app can be installed elsewhere. The macOS application registry is
    # more reliable than inferring absence from the two usual install folders.
    try:
        found = subprocess.run(["/usr/bin/open", "-Ra", "D-Bond"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=0.75, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {"installation": "unknown", "service": "unknown"}
    if found.returncode == 0:
        return {"installation": "installed", "service": "offline"}
    if found.returncode == 1:
        return {"installation": "uninstalled", "service": "offline"}
    return {"installation": "unknown", "service": "unknown"}
