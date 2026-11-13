#!/usr/bin/env python3
"""Read-only SessionStart reminder for an exactly enrolled Claude Code chat."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")


def _query(script: str, *args: str) -> dict | None:
    try:
        result = subprocess.run([sys.executable, script, *args], capture_output=True,
                                text=True, timeout=5, check=True)
        value = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def context(event: object, *, dbond_script: str) -> dict | None:
    if (not isinstance(event, dict) or event.get("hook_event_name") != "SessionStart"
            or event.get("source") not in ("startup", "resume")):
        return None
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not UUID.fullmatch(session_id):
        return None
    binding = _query(dbond_script, "session-binding", session_id, "--client", "claude")
    if not binding:
        return None
    alias, service_id = binding.get("alias"), binding.get("service_id")
    if (not isinstance(alias, str) or not ALIAS.fullmatch(alias)
            or not isinstance(service_id, str) or not UUID.fullmatch(service_id)):
        return None
    status = _query(dbond_script, "session-status", alias)
    if not status or status.get("alias") != alias or status.get("service_id") != service_id:
        return None
    if status.get("state") != "pending_listener":
        return None
    message = (
        "D-Bond listener for this exact paired Claude session is absent. "
        "On this session's next real turn, use the host's built-in Monitor to run "
        f"`dbond session-listen {alias}` with a 1800000 ms timeout. "
        f"Check `dbond session-status {alias}` afterward; report listening only "
        "if it says subscriber_present. Do not start a second Monitor when one "
        "is already active. When Monitor expires, recheck status and re-arm "
        "it if the session is still active. A stopped host cannot be awakened "
        "by this hook."
    )
    return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                   "additionalContext": message}}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dbond-script", default=str(Path(__file__).resolve().parents[1] / "server" / "dbond.py"))
    args = parser.parse_args()
    try:
        event = json.load(sys.stdin)
    except (ValueError, UnicodeError):
        return 0
    result = context(event, dbond_script=args.dbond_script)
    if result:
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
