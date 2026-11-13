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
