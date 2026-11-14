"""Attach the stable Mac receiver to one exact, live Claude Code session."""
import json
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True


def _helper():
    here = Path(__file__).resolve()
    bundles = [p for p in here.parents if p.suffix == ".app" and p.name == "D-Bond.app"]
    bundles.extend([Path.home() / "Applications/D-Bond.app", Path("/Applications/D-Bond.app")])
    for bundle in bundles:
        path = bundle / "Contents/Resources/agent-installers/claude-session/listener.py"
        if path.is_file():
            return path
    return None


def native_channel_opted_in(session_id):
    """Check exact verified host launch admission without reading host keys."""
    helper = _helper()
    if helper is None:
        return False
    source = helper.with_name("inbox.py")
    try:
        spec = importlib.util.spec_from_file_location("dbond_claude_host_inbox", source)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module.discover_session(session_id).get("native_channel") is True
    except Exception:
        return False


def status(session_id):
    """Read one exact receiver health snapshot without attaching or waking it."""
    if (not isinstance(session_id, str)
            or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", session_id)):
        return {"state": "blocked", "ready": False, "reason": "invalid_session_id"}
    helper = _helper()
    if helper is None:
        return {"state": "not_installed", "ready": False, "reason": "native_receiver_not_installed"}
    try:
        process = subprocess.run([sys.executable, "-B", str(helper), "status", "--thread-id", session_id, "--json"],
                                 stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, check=False)
        value = json.loads(process.stdout)
        if (not isinstance(value, dict) or (process.returncode != 0
                and (value.get("state") not in ("blocked", "unsupported") or value.get("ready") is not False))):
            raise ValueError("Invalid receiver health")
        result = {key: value[key] for key in ("state", "reason", "ready", "native", "inbox_supported",
                  "socket_verified", "receiver_kind", "last_delivery_state") if key in value}
        result.setdefault("state", "unconfirmed")
        result["ready"] = result.get("ready") is True and result.get("native") is True and result.get("inbox_supported") is True
        result["thread_id"] = session_id
        result["evidence"] = "native_receiver_health_snapshot"
        return result
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"state": "unconfirmed", "ready": False, "reason": "native_receiver_health_unavailable"}


def attach(alias, session_id, pairing=None):
    if (not isinstance(session_id, str)
            or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", session_id)
            or not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", alias)):
        return {"state": "blocked", "ready": False, "reason": "The exact Claude session and pairing alias are required."}
    helper = _helper()
    if helper is None:
        return {"state": "blocked", "mode": "native_session", "ready": False,
                "reason": "native_receiver_not_installed",
                "instruction": "Update the D-Bond Mac app to install the exact Claude receiver. Do not report connected or start a duplicate Monitor."}
    try:
        process = subprocess.run([sys.executable, "-B", str(helper), "install", "--thread-id", session_id,
                                  "--alias", alias, "--json"], stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=35, check=False)
        value = json.loads(process.stdout)
        if not isinstance(value, dict):
            raise ValueError("Invalid receiver status")
        # Configuration, inherited host capabilities and credentials stay private.
        result = {key: value[key] for key in ("state", "reason", "wakeState", "ready", "native",
                  "inbox_supported", "lastVerifiedAt", "receiver_kind") if key in value}
        result.setdefault("state", "unconfirmed")
        result["ready"] = result.get("ready") is True and result.get("native") is True and result.get("inbox_supported") is True
        result["mode"] = "native_channel" if result.get("state") == "native_channel_owned" else "native_session"
        result["thread_id"] = session_id
        result["verify_command"] = f"dbond session-status {alias} --finish-registration"
        if result["ready"]:
            instruction = "The exact Claude native receiver has attached. Do not start a timed Monitor."
        elif result["state"] == "waiting_host_bootstrap":
            instruction = ("Pairing is saved, but the native receiver has not started. "
                           f"Run `dbond session-listen {alias}` once as an ordinary Bash command inside this exact open Claude Code session "
                           "to bootstrap its host-owned receiver. Do not start a timed Monitor for this setup step.")
        elif result["state"] == "native_channel_owned":
            instruction = ("This exact Claude host opted into its native D-Bond channel. "
                           "Verify that channel's actual stream attachment; launch opt-in alone does not prove delivery. Do not start a timed Monitor.")
        elif result["state"] == "waiting_existing_listener":
            instruction = ("An existing listener still owns this pairing's streams. Do not create another Monitor. "
                           "Finish the old temporary listener, then refresh this exact native receiver and verify its status.")
        elif result["state"] in ("starting", "connecting"):
            instruction = "The exact Claude native receiver is starting and has not confirmed attachment. Do not start a timed Monitor."
        elif result["state"] == "unsupported":
            instruction = "This host does not provide the supported native inbox contract. Use the foreground fallback selected by enrollment."
        else:
            instruction = "The exact Claude native receiver is not ready. Check its reported state and reason; do not report connected or start a duplicate Monitor."
        result["instruction"] = (instruction + " Check session status after attachment, then END setup. "
                                 "A queued event is not a model wake; acknowledge an owner wake check only in its later receiving turn.")
        return result
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"state": "unconfirmed", "ready": False, "mode": "native_session",
                "reason": "Could not verify the exact Claude host receiver. Check the session in D-Bond; do not start a duplicate Monitor."}
