"""Attach the installed Mac listener to one exact, existing local Codex chat."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit


def _helper():
    # Run a long-lived service from a stable app location. A temporary checkout
    # or an extracted plugin cache can move during an ordinary update.
    here = Path(__file__).resolve()
    bundles = [p for p in here.parents if p.suffix == ".app" and p.name == "D-Bond.app"]
    bundles.extend([Path.home() / "Applications/D-Bond.app", Path("/Applications/D-Bond.app")])
    for bundle in bundles:
        path = bundle / "Contents/Resources/agent-installers/codex-session/listener.py"
        if path.is_file():
            return path
    return None


def status(thread_id, service_id):
    """Read the exact listener's public health; never attach or submit a turn."""
    valid = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
    if any(not isinstance(value, str) or not re.fullmatch(valid, value)
           for value in (thread_id, service_id)):
        return {"state": "blocked", "ready": False, "reason": "invalid_listener_identity"}
    helper = _helper()
    if helper is None:
        return {"state": "not_installed", "ready": False, "reason": "codex_listener_not_installed"}
    try:
        process = subprocess.run([sys.executable, "-B", str(helper), "status", "--thread-id", thread_id, "--json"],
                                 stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=8, check=False)
        if process.returncode != 0 or len(process.stdout) > 16384:
            raise ValueError("Invalid listener health")
        value = json.loads(process.stdout)
        if (not isinstance(value, dict) or value.get("thread_id") != thread_id
                or value.get("service_id") != service_id):
            raise ValueError("Listener identity differs from the pairing")
        result = {"thread_id": thread_id, "service_id": service_id,
                  "evidence": "codex_listener_health_snapshot"}
        for key in ("state", "reason", "host", "transport", "host_id"):
            if key in value:
                if value[key] is not None and (not isinstance(value[key], str) or len(value[key]) > 120):
                    raise ValueError("Invalid listener status field")
                result[key] = value[key]
        pending = value.get("pending", {})
        if not isinstance(pending, dict) or not set(pending).issubset({"direct", "dot"}):
            raise ValueError("Invalid pending streams")
        result["pending"] = {}
        fields = ("firstObservedAt", "ownerState", "ownerUnavailableFirstAt", "ownerUnavailableLastAt",
                  "ownerRecoveredFirstAt", "ownerRecoveredLastAt", "reason", "event_id")
        for lane, record in pending.items():
            if (not isinstance(record, dict) or type(record.get("sequence")) is not int
                    or record["sequence"] < 1):
                raise ValueError("Invalid pending message")
            item = {"sequence": record["sequence"]}
            for key in fields:
                if key in record:
                    if not isinstance(record[key], str) or len(record[key]) > 120:
                        raise ValueError("Invalid pending diagnostic")
                    item[key] = record[key]
            result["pending"][lane] = item
        result.setdefault("state", "unconfirmed")
        result["ready"] = (value.get("ready") is True and result.get("host") == "owner_available"
                           and result.get("transport") == "subscribed" and not pending)
        return result
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"state": "unconfirmed", "ready": False, "reason": "codex_listener_health_unavailable"}


def attach(alias, thread_id, pairing):
    canonical = re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", thread_id or "")
    if not canonical or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", alias):
        return {"state": "blocked", "reason": "The exact Codex chat and pairing alias are required."}
    helper = _helper()
    if not helper:
        return {"state": "blocked", "reason": "Update the D-Bond Mac app to install its session listener."}
    try:
        process = subprocess.run([sys.executable, "-B", str(helper), "install", "--thread-id", thread_id,
                                  "--alias", alias, "--json"], stdin=subprocess.DEVNULL, capture_output=True,
                                 text=True, timeout=35, check=False)
        value = json.loads(process.stdout)
        if not isinstance(value, dict):
            raise ValueError()
        # Do not expose the supervisor's private configuration or any credentials.
        result = {k: value[k] for k in ("state", "reason", "wakeState", "ready", "lastVerifiedAt") if k in value}
        result.setdefault("state", "blocked")
        result["service_id"] = urlsplit(pairing["endpoint"]).path.split("/")[-2]
        result["thread_id"] = thread_id
        return result
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"state": "unconfirmed", "reason": "Could not verify the Codex host listener. Check the session in D-Bond."}


def thread_from_request(args):
    explicit = args.get("thread_id")
    current = os.environ.get("CODEX_THREAD_ID", "").lower()
    # Shared MCP servers may serve several chats. The caller must identify its own
    # chat. A conventional per-thread alias is also an explicit address.
    alias = args.get("alias", "")
    alias_thread = alias[6:].lower() if alias.startswith("codex-") else ""
    valid = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
    if not re.fullmatch(valid, alias_thread):
        alias_thread = ""
    explicit = explicit.lower() if isinstance(explicit,str) else explicit
    if explicit is not None and not isinstance(explicit,str):
        raise ValueError("thread_id must be a Codex chat UUID")
    if explicit and alias_thread and explicit != alias_thread:
        raise ValueError("The Codex chat ID and alias identify different chats")
    thread = explicit or alias_thread
    if thread and not re.fullmatch(valid, thread):
        raise ValueError("Invalid Codex chat UUID")
    if current and thread and current != thread:
        raise ValueError("This host's Codex chat ID differs from the requested thread")
    return thread or (current if not alias or args.get("host") == "codex" else None)
