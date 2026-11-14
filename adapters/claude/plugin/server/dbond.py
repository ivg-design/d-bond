#!/usr/bin/env python3
"""Universal local sender/receiver for a D-Bond service pairing."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import getpass
import hashlib
import io
import json
import os
import re
import select
import socket
import stat
import subprocess
import sys
# The CLI also ships inside a signed macOS app; leave its resources read-only.
sys.dont_write_bytecode = True
import tempfile
import threading
import time
from datetime import datetime
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

APP_DIR = Path(os.environ["DBOND_PAIRING_DIR"]).expanduser() if os.environ.get("DBOND_PAIRING_DIR") else Path.home() / "Library/Application Support/D-Bond CLI"
OWNER_SOCKET = Path.home() / "Library/Application Support/D-Bond Proof of Concept/pair.sock"
ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
EVENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,119}$")
WAKE_ID_RE = re.compile(r"^@wake:[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
SESSION_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
MAX_PAIRING = 64 * 1024
MAX_FRAME = 512 * 1024


def _local_trace_time(raw: str) -> str:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        local = parsed.astimezone()
        return local.strftime("%b %-d, %Y, %-I:%M:%S %p %Z")
    except (TypeError, ValueError, OverflowError):
        return raw


def _format_trace(value: dict) -> dict:
    result = dict(value)
    if isinstance(result.get("events"), list):
        result["events"] = [_format_trace(event) for event in result["events"]]
    if isinstance(result.get("event"), dict):
        result["event"] = _format_trace(result["event"])
    if isinstance(result.get("stages"), list):
        result["stages"] = [dict(stage, at=_local_trace_time(stage["at"]))
                             if isinstance(stage, dict) and isinstance(stage.get("at"), str) else stage
                             for stage in result["stages"]]
    return result


def _owner_request(payload: dict) -> dict:
    # Keep the long-standing one-file CLI commands usable when invoked from an
    # older standalone copy; trace support loads its same-user socket helper lazily.
    from owner import request
    return request(payload)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def fail(message: str, code: int = 2) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def _safe_dir(create: bool = True) -> Path:
    home = Path.home()
    try:
        relative = APP_DIR.relative_to(home)
    except ValueError:
        fail("Pairing store must be inside this user's home directory.")
    if not relative.parts or ".." in relative.parts:
        fail("Pairing store must be a child directory inside this user's home directory.")
    current = home
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            fail("Pairing store path contains a symlink; refusing to use it.")
    current = APP_DIR
    if current.is_symlink():
        fail("Pairing store path contains a symlink; refusing to use it.")
    if not APP_DIR.exists():
        if not create:
            fail("No local pairings. Run `dbond link` first.")
        APP_DIR.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        APP_DIR.mkdir(mode=0o700)
    st = APP_DIR.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        fail("Pairing store must be a private owner-only directory.")
    return APP_DIR


def _alias_path(alias: str, create: bool = True) -> Path:
    if not ALIAS_RE.fullmatch(alias):
        fail("Alias must be 1–64 letters, digits, dots, underscores, or hyphens.")
    return _safe_dir(create) / f"{alias}.json"


def _atomic_json(path: Path, value: dict) -> None:
    if path.is_symlink():
        fail("Refusing to replace a symlink in the pairing store.")
    fd, temp = tempfile.mkstemp(prefix=".dbond-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, separators=(",", ":"))
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600, follow_symlinks=False)
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass


def _read_pairing(alias: str) -> dict:
    path = _alias_path(alias, create=False)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as src:
            st = os.fstat(src.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
                fail("Pairing file must be owner-only and owned by this user.")
            value = json.load(src)
            validated = validate_pairing(value)
            validated["cursor"] = value.get("cursor", 0)
            validated["direct_cursor"] = value.get("direct_cursor", 0)
            validated["pushes"] = value.get("pushes", [])
            return validated
    except FileNotFoundError:
        fail(f"No pairing named {alias!r}.")
    except (OSError, ValueError) as exc:
        fail(f"Could not read pairing: {exc}")


def validate_pairing(data: dict) -> dict:
    if not isinstance(data, dict):
        fail("Pairing input must be a JSON object.")
    endpoint, secret = data.get("endpoint"), data.get("secret")
    try:
        url = urllib.parse.urlsplit(endpoint)
        port = url.port or 80
    except (TypeError, ValueError):
        fail("Pairing needs the D-Bond endpoint and secret.")
    if (url.scheme != "http" or url.hostname != "127.0.0.1" or url.username or url.password
            or url.query or url.fragment or not (1 <= port <= 65535)
            or not re.fullmatch(r"/v1/services/[0-9a-fA-F-]{36}/events", url.path)
            or not isinstance(secret, str) or not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", secret)):
        fail("Use the loopback service endpoint and one-time credential from D-Bond.")
    pairing = {"endpoint": urllib.parse.urlunsplit(url), "secret": secret, "cursor": 0, "pushes": []}
    destination = data.get("destination")
    if destination is not None:
        if destination not in ("dot", "peer", "herald", "demo"):
            fail("Invalid pairing destination.")
        pairing["destination"] = destination
    client, session_id = data.get("client"), data.get("session_id")
    if client is not None or session_id is not None:
        if client not in ("codex", "claude") or not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
            fail("Invalid session binding in pairing.")
        pairing.update(client=client, session_id=session_id.lower())
    origin = data.get("chromeOrigin")
    if origin is not None:
        if not isinstance(origin, str) or not re.fullmatch(r"chrome-extension://[a-p]{32}", origin):
            fail("Invalid Chrome extension origin in pairing.")
        pairing["chromeOrigin"] = origin
    return pairing


def _load_source(path: str | None) -> dict:
    if path == "-" or (path is None and not sys.stdin.isatty()):
        raw = sys.stdin.read(MAX_PAIRING + 1)
    elif path:
        p = Path(path)
        if p.is_symlink():
            fail("Refusing symlinked pairing input.")
        fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as src:
            raw = src.read(MAX_PAIRING + 1)
    else:
        endpoint = input("D-Bond loopback endpoint: ").strip()
        secret = getpass.getpass("D-Bond one-time credential (hidden): ")
        return {"endpoint": endpoint, "secret": secret}
    if len(raw.encode("utf-8")) > MAX_PAIRING:
        fail("Pairing JSON is too large.")
    try:
        return json.loads(raw)
    except ValueError as exc:
        fail(f"Invalid pairing JSON: {exc}")


def _request(pairing: dict, method: str, endpoint: str, body: dict | None = None,
             timeout: float = 10) -> tuple[int, dict]:
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode() if body is not None else None
    req = urllib.request.Request(endpoint, data=raw, method=method, headers={
        "Authorization": "Bearer " + pairing["secret"],
        "Accept": "application/json",
        **({"Content-Type": "application/json"} if body is not None else {}),
    })
    try:
        with _OPENER.open(req, timeout=timeout) as res:
            data = res.read(1024 * 1024)
            return res.status, json.loads(data or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", "replace")
        fail(f"D-Bond HTTP {exc.code}: {detail}")
    except (OSError, urllib.error.URLError) as exc:
        fail(f"D-Bond request failed: {exc}")


class Stream:
    def __init__(self, pairing: dict, cursor: int, *, stream_suffix: str = "/stream", sequence_key: str = "sequence"):
        self.sequence_key = sequence_key
        parsed = urllib.parse.urlsplit(pairing["endpoint"])
        self.sock = socket.create_connection((parsed.hostname, parsed.port or 80), timeout=10)
        self.reader = self.sock.makefile("rb")
        path = parsed.path + stream_suffix + "?after=" + str(cursor)
        request = (f"GET {path} HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port or 80}\r\n"
                   f"Authorization: Bearer {pairing['secret']}\r\nAccept: text/event-stream\r\n"
                   "Connection: keep-alive\r\n\r\n")
        self.sock.sendall(request.encode("ascii"))
        status = self.reader.readline(4097).decode("ascii", "replace").strip()
        headers = {}
        total = 0
        while True:
            line = self.reader.readline(4097)
            total += len(line)
            if total > 8192 or not line:
                self.close()
                fail("Invalid D-Bond stream headers.")
            if line in (b"\r\n", b"\n"):
                break
            key, sep, value = line.decode("ascii", "replace").partition(":")
            if sep:
                headers[key.lower()] = value.strip()
        if " 423 " in status or " 503 " in status:
            self.close()
            raise ConnectionError("D-Bond is paused or unavailable.")
        if " 200 " not in status or not headers.get("content-type", "").startswith("text/event-stream"):
            self.close()
            fail("D-Bond did not open the authenticated receipt stream.")
        self.sock.settimeout(None)

    def next_event(self, deadline: float | None = None) -> dict | None:
        fields: dict[str, str] = {}
        while True:
            try:
                self.sock.settimeout(None if deadline is None else max(0.001, deadline - time.monotonic()))
                line = self.reader.readline(MAX_FRAME + 1)
            except socket.timeout:
                return None
            if len(line) > MAX_FRAME:
                fail("D-Bond stream frame exceeded its size limit.")
            if not line:
                return None
            if line in (b"\n", b"\r\n"):
                if "data" not in fields:
                    fields = {}
                    continue
                try:
                    event = json.loads(fields["data"])
                    sequence = int(fields["id"])
                    if event.get(self.sequence_key) != sequence:
                        fail("D-Bond stream sequence mismatch.")
                    return event
                except (ValueError, KeyError, TypeError):
                    fail("Invalid D-Bond stream event.")
            if line.startswith(b":"):
                continue
            name, sep, value = line.decode("utf-8", "strict").rstrip("\r\n").partition(":")
            if sep:
                fields[name] = value.lstrip(" ")

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.reader.close()
        self.sock.close()


def _save(alias: str, pairing: dict) -> None:
    path = _alias_path(alias)
    lock_path = path.with_suffix(".lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        lock_stat = os.fstat(lock_fd)
        if lock_stat.st_uid != os.getuid() or lock_stat.st_mode & 0o077:
            fail("Pairing lock must be owner-only and owned by this user.")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        merged = dict(pairing)
        if path.exists():
            existing = _read_pairing(alias)
            for key in ("client", "session_id"):
                if key in existing and key in pairing and existing[key] != pairing[key]:
                    fail("Pairing is already bound to another host session.")
                if key in existing:
                    merged.setdefault(key, existing[key])
            for key in ("destination", "chromeOrigin"):
                if key in existing:
                    merged.setdefault(key, existing[key])
            merged["cursor"] = max(int(existing.get("cursor", 0)), int(pairing.get("cursor", 0)))
            merged["direct_cursor"] = max(int(existing.get("direct_cursor", 0)), int(pairing.get("direct_cursor", 0)))
            merged["pushes"] = list(dict.fromkeys(existing.get("pushes", []) + pairing.get("pushes", [])))[-256:]
        _atomic_json(path, merged)
    finally:
        os.close(lock_fd)


def bind_session(alias: str, client: str, session_id: str) -> dict:
    if client not in ("claude", "codex") or not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("An exact supported host session UUID is required")
    pairing = _read_pairing(alias)
    canonical = session_id.lower()
    if pairing.get("session_id") and (pairing.get("client"), pairing.get("session_id")) != (client, canonical):
        raise ValueError("This pairing is bound to another host session")
    pairing.update(client=client, session_id=canonical)
    _save(alias, pairing)
    return {"state": "bound", "client": client, "session_id": canonical}


def session_binding(client: str, session_id: str) -> dict:
    if client not in ("claude", "codex") or not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("An exact supported host session UUID is required")
    matches = []
    store = _safe_dir(create=False)
    for path in store.glob("*.json"):
        if path.stem.startswith("request-") or not ALIAS_RE.fullmatch(path.stem):
            continue
        try:
            pairing = _read_pairing(path.stem)
        except SystemExit:
            continue
        if pairing.get("client") == client and pairing.get("session_id") == session_id.lower():
            service_id = pairing["endpoint"].split("/services/", 1)[1].split("/", 1)[0].lower()
            matches.append({"alias": path.stem, "service_id": service_id})
    if len(matches) != 1:
        raise ValueError("No unique exact D-Bond session binding")
    return matches[0]


def command_session_binding(args) -> None:
    try:
        print(json.dumps(session_binding(args.client, args.session_id), ensure_ascii=False))
    except ValueError as exc:
        fail(str(exc))


def command_link(args) -> None:
    pairing = validate_pairing(_load_source(args.file))
    _save(args.alias, pairing)
    print(json.dumps({"linked": args.alias, "endpoint": pairing["endpoint"]}))


def command_enroll(args) -> None:
    destination = "peer" if getattr(args, "direct", False) else getattr(args, "destination", None)
    if (destination != "herald" and getattr(args, "client", None) in (None, "claude")
            and not any(getattr(args, key, None) for key in ("name", "service", "topic", "title"))):
        origin = _initiating_claude()
        supplied = getattr(args, "session_id", None)
        if origin and supplied and origin[0].lower() != supplied.lower():
            fail("The supplied session ID differs from the verified Claude Code ancestor.")
        if origin:
            args.client, args.session_id = "claude", origin[0].lower()
            args.title = origin[1]
    if destination == "herald" and (getattr(args, "client", None) or getattr(args, "session_id", None)):
        fail("Choose a Herald service name to connect alerts, or use a session destination for an AI chat.")
    path = _alias_path(args.alias)
    if path.exists() or path.is_symlink():
        if destination == "herald" and not path.is_symlink():
            pair = _read_pairing(args.alias)
            if pair.get("destination") == "herald":
                own_id = pair["endpoint"].split("/services/", 1)[1].split("/", 1)[0].lower()
                if any(row.get("id") == own_id and row.get("name") == enrollment_name(args) for row in _peers(pair)):
                    print(json.dumps(_enrollment_payload(args, pair, reused=True), ensure_ascii=False))
                    return
        session_id = getattr(args, "session_id", None)
        if (getattr(args, "client", None) == "claude" and isinstance(session_id, str)
                and SESSION_ID_RE.fullmatch(session_id) and not path.is_symlink()):
            pair = _read_pairing(args.alias)
            own_id = pair["endpoint"].split("/services/", 1)[1].split("/", 1)[0].lower()
            service = next((row for row in _owner_request({"op":"list"}).get("services", [])
                            if row.get("id") == own_id), None)
            if service and service.get("name", "").startswith("Claude / "):
                bind_session(args.alias, "claude", session_id)
                print(json.dumps(_enrollment_payload(args, pair, reused=True), ensure_ascii=False))
                return
        if (getattr(args, "client", None) == "codex" and isinstance(session_id, str)
                and SESSION_ID_RE.fullmatch(session_id) and not path.is_symlink()):
            pairing = _read_pairing(args.alias)
            canonical = session_id.lower()
            bound = pairing.get("client") == "codex" and pairing.get("session_id") == canonical
            conventional = args.alias.casefold() == f"codex-{canonical}"
            if conventional and not bound:
                own_id = pairing["endpoint"].split("/services/", 1)[1].split("/", 1)[0].lower()
                matches = [row for row in _peers(pairing) if row.get("id", "").lower() == own_id]
                conventional = len(matches) == 1 and isinstance(matches[0].get("name"), str) and matches[0]["name"].startswith("Codex / ")
            if bound or conventional:
                print(json.dumps(_enrollment_payload(args, pairing, reused=True), ensure_ascii=False))
                return
        fail(f"Alias {args.alias!r} already exists; choose a new alias.")
    lock_path = path.with_suffix(".enrolling")
    try:
        lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        fail(f"Alias {args.alias!r} is already being enrolled.")
    os.close(lock_fd)
    try:
        base = OWNER_SOCKET.parent
        try:
            relative = base.relative_to(Path.home())
        except ValueError:
            fail("D-Bond pairing socket is outside the user home directory.")
        current = Path.home()
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                fail("D-Bond pairing socket path contains a symlink.")
        st = base.stat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            fail("D-Bond pairing directory must be private and owned by this user.")
        socket_stat = OWNER_SOCKET.lstat()
        if not stat.S_ISSOCK(socket_stat.st_mode) or socket_stat.st_uid != os.getuid() or socket_stat.st_mode & 0o077:
            fail("D-Bond owner pairing socket is missing or has unsafe ownership/permissions.")
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(3)
        try:
            client.connect(str(OWNER_SOCKET))
            name = enrollment_name(args)
            frame = {"op": "pair", "name": name}
            if destination is not None:
                frame["destination"] = destination
            chrome_id = getattr(args, "chrome_id", None)
            if chrome_id is not None:
                if not re.fullmatch(r"[a-p]{32}", chrome_id):
                    fail("Expected an exact 32-letter Chrome extension ID (a through p).")
                frame["chromeOrigin"] = "chrome-extension://" + chrome_id
            request = json.dumps(frame, separators=(",", ":")).encode() + b"\n"
            client.sendall(request)
            data = bytearray()
            while len(data) <= MAX_PAIRING and not data.endswith(b"\n"):
                chunk = client.recv(4096)
                if not chunk:
                    break
                data.extend(chunk)
        except OSError as exc:
            fail(f"Could not enroll with the running D-Bond app: {exc}")
        finally:
            client.close()
        if len(data) > MAX_PAIRING or not data.endswith(b"\n"):
            fail("D-Bond returned an incomplete enrollment response.")
        try:
            value = json.loads(data)
        except ValueError:
            fail("D-Bond returned an invalid enrollment response.")
        if "error" in value:
            fail("D-Bond refused enrollment. Use a new display name or inspect the app.")
        pairing = validate_pairing(value)
        if getattr(args, "client", None) == "codex" and SESSION_ID_RE.fullmatch(getattr(args, "session_id", "") or ""):
            pairing.update(client="codex", session_id=args.session_id.lower())
        _atomic_json(path, pairing)
        print(json.dumps(_enrollment_payload(args, pairing, reused=False), ensure_ascii=False))
    finally:
        try:
            os.unlink(lock_path)
        except FileNotFoundError:
            pass


def _enrollment_payload(args, pairing: dict, *, reused: bool) -> dict:
    client, session_id = getattr(args, "client", None), getattr(args, "session_id", None)
    if client in ("claude", "codex") and isinstance(session_id, str) and SESSION_ID_RE.fullmatch(session_id):
        bind_session(args.alias, client, session_id)
        pairing = _read_pairing(args.alias)
    result = {"linked": args.alias, "endpoint": pairing["endpoint"],
              "reused": reused, "pairing_state": "saved"}
    if pairing.get("destination") == "herald":
        result["destination"] = "herald"
        return result
    result["listener"] = _enrollment_listener(args, pairing)
    if client in ("claude", "codex") and isinstance(session_id, str) and SESSION_ID_RE.fullmatch(session_id):
        ready = result["listener"].get("ready") is True
        status = session_status(args.alias, finish_registration=ready)
        result["wake"] = registration_wake_status(status)
        if ready:
            result["registration_finish"] = status.get("registration_finish")
    return result


def registration_wake_status(status: dict) -> dict:
    """Report broker state; registration schedules its single check after attachment."""
    finish = status.get("registration_finish") or {}
    result = {"state": finish.get("wakeState") or status.get("wakeState") or "unknown"}
    for source, target in (("lastWakeVerifiedAt", "lastVerifiedAt"), ("lastWakeVerifiedLocal", "lastVerifiedLocal")):
        if status.get(source):
            result[target] = status[source]
    return result


def _enrollment_listener(args, pairing: dict) -> dict:
    if getattr(args, "client", None) == "claude" and SESSION_ID_RE.fullmatch(getattr(args, "session_id", "") or ""):
        return claude_session_listener(args.alias, args.session_id.lower(), pairing)
    if getattr(args, "client", None) == "codex" and SESSION_ID_RE.fullmatch(getattr(args, "session_id", "") or ""):
        try:
            from codex_registration import attach
            return attach(args.alias, args.session_id.lower(), pairing)
        except Exception as exc:
            return {"state": "blocked", "reason": type(exc).__name__,
                    "instruction": "Pairing was saved, but the Codex host listener could not be attached. Do not report the session as connected."}
    return session_listener_guidance(args.alias, direct_only=pairing.get("destination") == "peer" or bool(getattr(args, "direct", False)))


def claude_session_listener(alias: str, session_id: str, pairing: dict) -> dict:
    try:
        from claude_registration import attach
        listener = attach(alias, session_id, pairing)
    except Exception as exc:
        return {"state": "unconfirmed", "mode": "native_session", "ready": False,
                "reason": type(exc).__name__,
                "instruction": "Pairing was saved, but the exact Claude receiver could not be verified. Do not report connected or start a duplicate Monitor."}
    if listener.get("state") == "unsupported":
        guidance = session_listener_guidance(alias, direct_only=pairing.get("destination") == "peer")
        return {**guidance, "native_receiver": listener}
    return listener


def _valid_title(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value or len(value) > 190 or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        fail("Session title must be 1–190 printable characters.")
    return value


def _claude_session_title(session_id: str, home: Path | None = None) -> str | None:
    """Read title metadata for one exact Claude session ID; never read prompt text."""
    home = home or Path.home()
    custom_title = ai_title = None
    projects = home / ".claude" / "projects"
    # Match only files whose basename is the supplied ID. No fuzzy lookup or transcript scan.
    if projects.is_dir():
        for transcript in projects.glob(f"*/{session_id}.jsonl"):
            try:
                with transcript.open("r", encoding="utf-8") as src:
                    for line in src:
                        try:
                            row = json.loads(line)
                        except (ValueError, TypeError):
                            continue
                        if not isinstance(row, dict):
                            continue
                        kind = row.get("type")
                        if kind == "custom-title" and isinstance(row.get("customTitle"), str):
                            custom_title = _valid_title(row["customTitle"])
                        elif kind == "ai-title" and isinstance(row.get("aiTitle"), str):
                            ai_title = _valid_title(row["aiTitle"])
            except (OSError, UnicodeError):
                continue

    return custom_title or ai_title or _claude_desktop_title(session_id, home)


def _claude_desktop_title(session_id: str, home: Path | None = None) -> str | None:
    """Use only the exact host UUID in Desktop's opaque sidecar title records."""
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        return None
    home = home or Path.home()
    metadata = home / "Library" / "Application Support" / "Claude" / "claude-code-sessions"
    if metadata.is_dir():
        for sidecar in metadata.glob("*/*/local_*.json"):
            try:
                if sidecar.is_symlink() or sidecar.stat().st_size > 1048576:
                    continue
                with sidecar.open("r", encoding="utf-8") as src:
                    row = json.load(src)
                if isinstance(row, dict) and row.get("cliSessionId") == session_id:
                    candidate = row.get("title")
                    if isinstance(candidate, str):
                        return _valid_title(candidate)
            except (OSError, UnicodeError, ValueError):
                continue
    return None


def _initiating_claude(start_pid=None):
    """Join only this CLI's ancestors to same-UID Claude live-session records.
    Check process start as well as PID: an old file cannot name a reused PID.
    """
    pid=start_pid or os.getppid()
    root=Path.home()/".claude/sessions"
    for _ in range(32):
        if pid<=1: break
        try:
            raw=subprocess.check_output(["/bin/ps","-p",str(pid),"-o","ppid=,uid=,lstart="],text=True,stderr=subprocess.DEVNULL,timeout=2,env=dict(os.environ,LC_ALL="C",TZ="UTC"))
            parent,uid,stamp=raw.strip().split(None,2)
            if int(uid)!=os.getuid(): break
            file=root/f"{pid}.json"
            if file.is_file() and not file.is_symlink() and file.stat().st_uid==os.getuid() and file.stat().st_size<1048576:
                rec=json.loads(file.read_bytes()); sid=rec.get("sessionId","")
                if rec.get("pid")==pid and rec.get("procStart","").split()==stamp.split() and SESSION_ID_RE.fullmatch(sid):
                    return sid, _valid_title(rec.get("name")) if isinstance(rec.get("name"),str) and rec["name"].strip() else _claude_session_title(sid)
            pid=int(parent)
        except (OSError,ValueError,TypeError,subprocess.SubprocessError): break
    return None

def enrollment_name(args) -> str:
    service = getattr(args, "service", None)
    topic = getattr(args, "topic", None)
    if service is not None or topic is not None:
        if not (service and topic) or any(getattr(args, key, None) for key in ("name", "client", "title", "session_id")):
            fail("Use --service and --topic together, or use the session title options.")
        service, topic = _valid_title(service), _valid_title(topic)
        name = f"{service} / {topic}"
        if " / " in service or len(name) > 200:
            fail("Use a service name without ' / ' and keep the full connection name within 200 characters.")
        return name
    client = getattr(args, "client", None)
    session_id = getattr(args, "session_id", None)
    title = _valid_title(getattr(args, "title", None))
    if not session_id and not title and not getattr(args,"name",None) and client in (None,"claude"):
        origin=_initiating_claude()
        if origin: return "Claude / " + (origin[1] or args.alias)
    if not client and not session_id and not title:
        return getattr(args,"name",None) or args.alias
    if client not in ("claude", "codex") or not session_id or not SESSION_ID_RE.fullmatch(session_id):
        fail("Use --client claude|codex with that client's exact UUID --session-id.")
    if client == "claude" and not title:
        title = _claude_session_title(session_id)
    # Codex DB cache prompt text is not a title source. Require a host-reported title
    # or use an explicit, non-content fallback tied to the exact session ID.
    title = title or getattr(args, "alias", None) or getattr(args, "name", None) or "Session"
    return f"{client.title()} / {title}"


def _peers(pairing: dict) -> list[dict]:
    url = pairing["endpoint"].removesuffix("/events") + "/peers"
    status, body = _request(pairing, "GET", url, timeout=10)
    services = body.get("services", []) if isinstance(body, dict) else []
    if status != 200 or not isinstance(services, list):
        fail("Could not read the D-Bond connection list.")
    return services


def _direct_url(pairing: dict) -> str:
    return pairing["endpoint"].removesuffix("/events") + "/messages"


def _direct_peers(pairing: dict) -> list[dict]:
    status, body = _request(pairing, "GET", _direct_url(pairing) + "/peers")
    if status != 200 or not isinstance(body.get("services"), list):
        fail("Could not list direct D-Bond recipients.")
    return body["services"]


def _direct_target(pairing: dict, value: str) -> str:
    peers = _direct_peers(pairing)
    matches = [row for row in peers if row.get("id") == value.lower() or row.get("name", "").casefold() == value.casefold()]
    if len(matches) != 1 or matches[0].get("paused") is True:
        fail("Use one active direct recipient name or ID from `dbond direct-peers`.")
    return matches[0]["id"]


def direct_delivery_status(pairing: dict, message_id: str, sequence: int, *, wait_ms: int = 0) -> dict:
    """Wait on authenticated receipt metadata, never on a conversational answer."""
    if (not isinstance(message_id, str) or not EVENT_RE.fullmatch(message_id)
            or type(sequence) is not int or sequence < 1
            or type(wait_ms) is not int or not 0 <= wait_ms <= 60000):
        raise ValueError("Invalid direct delivery status query")
    query = urllib.parse.urlencode({"id": message_id, "sequence": sequence,
                                    "condition": "acknowledged", "timeout_ms": wait_ms})
    code, result = _request(pairing, "GET", _direct_url(pairing) + "/status?" + query,
                            timeout=wait_ms / 1000 + 2)
    if (code != 200 or result.get("id") != message_id or result.get("sequence") != sequence
            or result.get("state") not in ("pending", "received", "acknowledged", "unconfirmed")
            or type(result.get("satisfied")) is not bool):
        raise ValueError("Invalid direct delivery status")
    return result


def direct_delivery_summary(pairing: dict, message_id: str, sequence: int, *, wait_ms: int = 0) -> dict:
    value = direct_delivery_status(pairing, message_id, sequence, wait_ms=wait_ms)
    return {**{key: value[key] for key in ("id", "to", "sequence", "basis", "adapter") if key in value},
            "state": "queued" if value["state"] == "pending" else value["state"]}


def direct_send_message(pairing: dict, to: str, text: str, *, message_id: str | None = None,
                        in_reply_to: str | None = None, wait_ms: int = 10000) -> dict:
    target = _direct_target(pairing, to)
    message_id = message_id or str(uuid.uuid4())
    if not EVENT_RE.fullmatch(message_id) or message_id.startswith("@dot:"):
        raise ValueError("Direct message ID is invalid or reserved")
    if type(wait_ms) is not int or not 0 <= wait_ms <= 60000:
        raise ValueError("wait_ms must be from 0 to 60000")
    body = {"id": message_id, "to": target, "text": text}
    if in_reply_to is not None:
        body["in_reply_to"] = in_reply_to
    try:
        code, stored = _request(pairing, "POST", _direct_url(pairing), body)
    except (OSError, ValueError, SystemExit) as exc:
        raise ValueError(f"Direct message {message_id} is unconfirmed; reconcile delivery and retry only with this same ID") from exc
    if (code not in (200, 202) or stored.get("id") != message_id
            or type(stored.get("sequence")) is not int or stored["sequence"] < 1):
        raise ValueError("Invalid direct-message storage receipt")
    result = {"id": message_id, "to": target, "sequence": stored["sequence"],
              "duplicate": stored.get("duplicate", False), "state": "queued"}
    try:
        # One bounded condition wait; an offline recipient leaves the durable
        # message queued. Retry uses the same ID and cannot create another send.
        with contextlib.redirect_stderr(io.StringIO()):
            delivery = direct_delivery_status(pairing, message_id, stored["sequence"], wait_ms=wait_ms)
        if delivery["satisfied"] and delivery["state"] == "acknowledged":
            result["state"] = "acknowledged"
        elif delivery["state"] == "received":
            result["state"] = "received"
        elif delivery["state"] == "unconfirmed":
            result["state"] = "unconfirmed"
        for key in ("basis", "adapter"):
            if key in delivery:
                result[key] = delivery[key]
    except (OSError, ValueError, SystemExit):
        result["delivery_status"] = "unconfirmed"
    return result


_RECEIPT_IN_FLIGHT_LOCK = threading.Lock()
_RECEIPT_IN_FLIGHT: dict[Path, int] = {}


def direct_delivery_receipt(pairing: dict, event: dict, *, adapter: str,
                            state: str = "received", basis: str = "host_acceptance") -> dict:
    """Durably queue metadata only, then authenticate as the exact recipient.

    Pipe/notification writes are received/host_acceptance. Only a proven host
    submission or a model-facing receipt call may acknowledge the delivery.
    """
    sender = event.get("from", event.get("sender_id"))
    source = event.get("id", event.get("source_id"))
    sequence = event.get("sequence")
    own_id = pairing["endpoint"].split("/services/", 1)[1].split("/", 1)[0]
    if (not isinstance(sender, str) or (sender != "owner" and not SESSION_ID_RE.fullmatch(sender))
            or not valid_direct_source(source, sender) or type(sequence) is not int or sequence < 1
            or ("to" in event and event["to"] != own_id)
            or state not in ("received", "acknowledged", "unconfirmed")
            or basis not in ("host_acceptance", "participant", "host_result_unknown")
            or (state == "received" and basis != "host_acceptance")
            or ((state == "unconfirmed") != (basis == "host_result_unknown"))
            or not isinstance(adapter, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", adapter)):
        raise ValueError("Invalid direct delivery receipt")
    body = {"from": sender, "id": source, "sequence": sequence, "state": state,
            "adapter": adapter, "basis": basis}
    queued = {"endpoint": pairing["endpoint"], "body": body}
    digest = hashlib.sha256(json.dumps(queued, sort_keys=True).encode()).hexdigest()
    path = _safe_dir(create=True) / (".receipt-" + digest + ".json")
    # The directory watcher can see the durable outbox before this first POST
    # finishes. Leave that in-flight attempt alone; its failure signals retry.
    with _RECEIPT_IN_FLIGHT_LOCK:
        _RECEIPT_IN_FLIGHT[path] = _RECEIPT_IN_FLIGHT.get(path, 0) + 1
    try:
        _atomic_json(path, queued)
        result = _deliver_direct_receipt(pairing, path, body)
    finally:
        with _RECEIPT_IN_FLIGHT_LOCK:
            if _RECEIPT_IN_FLIGHT[path] == 1:
                del _RECEIPT_IN_FLIGHT[path]
            else:
                _RECEIPT_IN_FLIGHT[path] -= 1
    if result["state"] == "receipt_queued":
        _signal_receipt_retry(pairing)
    return result


def _deliver_direct_receipt(pairing: dict, path: Path, body: dict) -> dict:
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            code, result = _request(pairing, "POST", _direct_url(pairing) + "/receipt", body, timeout=2)
        if (code != 200 or not isinstance(result.get("receipt"), dict)
                or any(result["receipt"].get(key) != value for key, value in body.items())):
            raise ValueError("Invalid direct delivery receipt response")
        path.unlink(missing_ok=True)
        return {"state": body["state"], "basis": body["basis"], "duplicate": result.get("duplicate", False)}
    except (OSError, ValueError, SystemExit):
        return {"state": "receipt_queued", "basis": body["basis"]}


def retry_direct_receipts(pairing: dict) -> int:
    """Replay an owner-only outbox without repeating the message or its host push."""
    pending = 0
    for path in _safe_dir(create=True).glob(".receipt-*.json"):
        try:
            with _RECEIPT_IN_FLIGHT_LOCK:
                if path in _RECEIPT_IN_FLIGHT:
                    continue
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "r", encoding="utf-8") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    continue
                value = json.load(source)
            if value.get("endpoint") == pairing["endpoint"] and isinstance(value.get("body"), dict):
                if _deliver_direct_receipt(pairing, path, value["body"])["state"] == "receipt_queued":
                    pending += 1
        except (OSError, ValueError, TypeError):
            continue
    return pending


_RECEIPT_WAKES_LOCK = threading.Lock()
_RECEIPT_WAKES: dict[str, list] = {}


def _signal_receipt_retry(pairing: dict) -> None:
    with _RECEIPT_WAKES_LOCK:
        for wake in _RECEIPT_WAKES.get(pairing["endpoint"], ()):
            wake.notify()


class _ReceiptRetryWake:
    """One exact binding's idle notification, with cross-process macOS recovery."""

    def __init__(self, pairing: dict, stop: threading.Event):
        self.endpoint = pairing["endpoint"]
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.closed = False
        self.kqueue = None
        self.read_fd = self.write_fd = self.directory_fd = None
        if sys.platform == "darwin" and hasattr(select, "kqueue"):
            self.read_fd, self.write_fd = os.pipe()
            os.set_blocking(self.read_fd, False)
            os.set_blocking(self.write_fd, False)
            self.directory_fd = os.open(_safe_dir(create=True), os.O_RDONLY)
            self.kqueue = select.kqueue()
            self.kqueue.control([
                select.kevent(self.read_fd, filter=select.KQ_FILTER_READ,
                              flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR),
                select.kevent(self.directory_fd, filter=select.KQ_FILTER_VNODE,
                              flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                              fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND | select.KQ_NOTE_ATTRIB
                              | select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE),
            ], 0, 0)
        with _RECEIPT_WAKES_LOCK:
            _RECEIPT_WAKES.setdefault(self.endpoint, []).append(self)

        def shutdown_wake():
            stop.wait()
            self.notify()

        threading.Thread(target=shutdown_wake, daemon=True).start()

    def notify(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.event.set()
            if self.write_fd is not None:
                try:
                    os.write(self.write_fd, b"\0")
                except (OSError, BlockingIOError):
                    pass

    def wait(self, timeout: float | None) -> None:
        if self.kqueue is None:
            self.event.wait(timeout)
        else:
            self.kqueue.control([], 8, timeout)
            try:
                while os.read(self.read_fd, 4096):
                    pass
            except BlockingIOError:
                pass
        self.event.clear()

    def close(self) -> None:
        with _RECEIPT_WAKES_LOCK:
            registered = _RECEIPT_WAKES.get(self.endpoint, [])
            if self in registered:
                registered.remove(self)
            if not registered:
                _RECEIPT_WAKES.pop(self.endpoint, None)
        with self.lock:
            self.closed = True
            if self.kqueue is not None:
                self.kqueue.close()
            for fd in (self.read_fd, self.write_fd, self.directory_fd):
                if fd is not None:
                    os.close(fd)


def receipt_retry_loop(pairing: dict, stop: threading.Event) -> None:
    # Register first, then scan once: an outbox item created concurrently cannot
    # fall into a gap between startup recovery and the indefinite idle wait.
    wake = _ReceiptRetryWake(pairing, stop)
    delay = 1.0
    try:
        while not stop.is_set():
            pending = retry_direct_receipts(pairing)
            if stop.is_set():
                break
            wake.wait(delay if pending else None)
            delay = min(30.0, delay * 2) if pending else 1.0
    finally:
        wake.close()


def command_direct_peers(args) -> None:
    print(json.dumps(_direct_peers(_read_pairing(args.alias)), ensure_ascii=False))


def command_direct_send(args) -> None:
    pairing = _read_pairing(args.alias)
    try:
        result = direct_send_message(pairing, args.to, args.text, message_id=args.message_id,
                                     in_reply_to=getattr(args, "in_reply_to", None),
                                     wait_ms=getattr(args, "wait_ms", 10000))
    except ValueError as exc:
        fail(str(exc))
    print(json.dumps(result, ensure_ascii=False))


def command_direct_ack(args) -> None:
    result = direct_delivery_receipt(_read_pairing(args.alias),
                                    {"from": args.sender_id, "id": args.source_id, "sequence": args.sequence},
                                    adapter="cli-agent", state="acknowledged", basis="participant")
    print(json.dumps(result, ensure_ascii=False))


def command_direct_status(args) -> None:
    print(json.dumps(direct_delivery_summary(_read_pairing(args.alias), args.message_id,
                                           args.sequence, wait_ms=args.wait_ms), ensure_ascii=False))


def direct_receipts(pairing: dict, after: int = 0):
    """Yield sender-scoped metadata only; consumers must not inject it into chat."""
    if type(after) is not int or after < 0:
        raise ValueError("after must be a nonnegative receipt sequence")
    own_id = pairing["endpoint"].split("/services/", 1)[1].split("/", 1)[0]
    stream = Stream({**pairing, "endpoint": _direct_url(pairing)}, after,
                    stream_suffix="/receipts-stream", sequence_key="receipt_sequence")
    try:
        while True:
            row = stream.next_event()
            if row is None:
                return
            if (not isinstance(row, dict) or set(row) != {"id", "from", "to", "sequence", "receipt_sequence",
                                                          "state", "adapter", "basis", "recorded_at"}
                    or row["from"] != own_id or not isinstance(row["to"], str)
                    or not SESSION_ID_RE.fullmatch(row["to"])
                    or not isinstance(row["id"], str) or not EVENT_RE.fullmatch(row["id"])
                    or type(row["sequence"]) is not int or row["sequence"] < 1
                    or type(row["receipt_sequence"]) is not int or row["receipt_sequence"] < 1
                    or row["state"] not in ("received", "acknowledged", "unconfirmed")
                    or row["basis"] not in ("host_acceptance", "participant", "connection_reply", "host_result_unknown")
                    or not isinstance(row["adapter"], str) or not isinstance(row["recorded_at"], str)):
                raise ValueError("Invalid sender delivery receipt metadata")
            if row["receipt_sequence"] > after:
                after = row["receipt_sequence"]
                yield row
    finally:
        stream.close()


def command_direct_receipts(args) -> None:
    pairing = _read_pairing(args.alias)
    cursor = args.after
    delay = 0.5
    try:
        while True:
            try:
                for row in direct_receipts(pairing, cursor):
                    print(json.dumps(row, ensure_ascii=False, separators=(",", ":")), flush=True)
                    cursor = row["receipt_sequence"]
                    delay = 0.5
            except (OSError, ConnectionError):
                pass
            time.sleep(delay)
            delay = min(20.0, delay * 2)
    except KeyboardInterrupt:
        return


def command_dot_inbox(args) -> None:
    if args.after < 0 or args.through < args.after:
        fail("Expected 0 <= after <= through.")
    pairing = _read_pairing(args.alias)
    after = args.after
    while True:
        params = urllib.parse.urlencode({"after": after, "through": args.through})
        code, page = _request(pairing, "GET", f"{pairing['endpoint']}/history?{params}")
        messages = page.get("messages")
        last = page.get("lastSequence")
        if (code != 200 or not isinstance(messages, list) or type(last) is not int
                or not after <= last <= args.through):
            fail("Invalid D-Bond history page.")
        for row in messages:
            if (row.get("state") != "dot_push" or not re.fullmatch(r"@dot:[0-9]+", row.get("id", ""))
                    or not after < row["sequence"] <= last):
                fail("Invalid addressed Dot history entry.")
            print(json.dumps({"id":row["id"], "sequence":row["sequence"], "text":row["reply"]}, ensure_ascii=False))
        if page.get("hasMore") is False:
            return
        if page.get("hasMore") is not True or last <= after:
            fail("Invalid D-Bond history cursor.")
        after = last


def command_direct_inbox(args) -> None:
    pairing = _read_pairing(args.alias)
    after = pairing.get("direct_cursor", 0) if args.after is None else args.after
    if type(after) is not int or after < 0:
        fail("Direct inbox cursor must be a nonnegative sequence.")
    status, body = _request(pairing, "GET", _direct_url(pairing) + "?after=" + str(after), timeout=15)
    if status != 200 or not isinstance(body.get("messages"), list):
        fail("D-Bond returned an invalid direct inbox.")
    rows = body["messages"]
    print(json.dumps({"messages": rows, "after": after}, ensure_ascii=False), flush=True)
    for event in rows:
        direct_delivery_receipt(pairing, event, adapter="cli-inbox")
    if args.advance and rows:
        pairing["direct_cursor"] = max(row["sequence"] for row in rows)
        _save(args.alias, pairing)


def command_direct_listen(args) -> None:
    pairing = _read_pairing(args.alias)
    stop = threading.Event()
    threading.Thread(target=receipt_retry_loop, args=(pairing, stop), daemon=True).start()
    cursor = pairing.get("direct_cursor", 0)
    delay = 0.5
    try:
        while True:
            stream = None
            try:
                stream = Stream({**pairing, "endpoint": _direct_url(pairing)}, cursor)
                while True:
                    event = stream.next_event()
                    if event is None:
                        raise ConnectionError("Direct stream closed.")
                    print(json.dumps(event, ensure_ascii=False), flush=True)
                    direct_delivery_receipt(pairing, event, adapter="cli-listener")
                    cursor = pairing["direct_cursor"] = event["sequence"]
                    _save(args.alias, pairing)
                    delay = 0.5
            except (OSError, ConnectionError):
                time.sleep(delay)
                delay = min(20, delay * 2)
            finally:
                if stream:
                    stream.close()
    except KeyboardInterrupt:
        return
    finally:
        stop.set()


def session_listener_guidance(alias: str, *, direct_only: bool = False) -> dict:
    if not ALIAS_RE.fullmatch(alias):
        raise ValueError("Invalid pairing alias")
    command = f"dbond session-listen {alias}"
    streams = ["direct"] if direct_only else ["direct", "dot"]
    return {"command": command, "mode": "foreground", "streams": streams,
            "verify_command": f"dbond session-status {alias} --finish-registration",
            "instruction": f"In this registration turn, start the host's native Monitor on {command} for this exact paired session, then run dbond session-status {alias} --finish-registration after the requested streams attach, then END setup. A verified pairing is ready. If a wake check is pending, acknowledge its broker sequence when it arrives in the receiving turn. Keep the Monitor running; saved cursors replay after absence. This command alone does not wake a closed chat."}


def valid_direct_source(source: str, sender: str) -> bool:
    return (isinstance(source, str) and
            ((bool(EVENT_RE.fullmatch(source)) and not source.startswith("@dot:"))
             or (sender == "owner" and bool(WAKE_ID_RE.fullmatch(source)))))


def session_status(alias: str, *, finish_registration: bool = False) -> dict:
    if type(finish_registration) is not bool:
        raise ValueError("finish_registration must be a boolean")
    result = _session_status_readonly(alias)
    if not finish_registration:
        return result
    if result.get("state") != "subscriber_present":
        return {**result, "registration_finish": {"scheduled": False, "state": "listener_not_ready"}}
    try:
        reply = _owner_request({"op": "finish_registration", "id": result["service_id"]})
        if (not isinstance(reply, dict) or not isinstance(reply.get("id"), str)
                or reply["id"].lower() != result["service_id"]
                or type(reply.get("scheduled")) is not bool
                or reply.get("wakeState") not in ("pending", "verified", "offline")):
            raise ValueError("Invalid owner registration finish response")
        finish = {"scheduled": reply["scheduled"], "wakeState": reply["wakeState"],
                  "state": "scheduled" if reply["scheduled"] else "not_scheduled"}
    except (OSError, ValueError, SystemExit):
        finish = {"scheduled": False, "state": "unknown"}
    return {**result, "registration_finish": finish}


def _session_status_readonly(alias: str) -> dict:
    pairing = _read_pairing(alias)
    service_id = pairing["endpoint"].split("/services/", 1)[1].split("/", 1)[0].lower()
    try:
        response = _owner_request({"op": "list"})
    except (OSError, ValueError, SystemExit):
        return {"alias": alias, "service_id": service_id, "state": "unknown",
                "evidence": "owner_socket_unavailable"}
    services = response.get("services") if isinstance(response, dict) else None
    if not isinstance(services, list):
        return {"alias": alias, "service_id": service_id, "state": "unknown",
                "evidence": "owner_socket_invalid_response"}
    matches = [row for row in services if isinstance(row, dict)
               and isinstance(row.get("id"), str) and row["id"].lower() == service_id]
    if len(matches) != 1:
        return {"alias": alias, "service_id": service_id, "state": "revoked",
                "evidence": "owner_socket_connection_absent"}
    row = matches[0]
    destination = row.get("destination")
    required = ["direct"] if destination == "peer" else ["direct", "dot"] if destination == "dot" else []
    direct_count, dot_count = row.get("directSubscribers"), row.get("subscribers")
    if (not required or type(direct_count) is not int or direct_count < 0
            or type(dot_count) is not int or dot_count < 0):
        state = "unknown"
    elif row.get("paused") is True:
        state = "paused"
    elif direct_count > 0 and ("dot" not in required or dot_count > 0):
        state = "subscriber_present"
    else:
        state = "pending_listener"
    wake_state = row.get("wakeState")
    if wake_state not in ("pending", "verified", "offline"):
        wake_state = "unknown"
    verified_at = row.get("lastVerifiedAt", row.get("lastWakeVerifiedAt"))
    if not isinstance(verified_at, str) or len(verified_at) > 64:
        verified_at = None
    result = {"alias": alias, "service_id": service_id, "state": state,
            "required_streams": required, "direct_subscribers": direct_count,
            "dot_subscribers": dot_count, "wakeState": wake_state,
            "lastWakeVerifiedAt": verified_at, "evidence": "owner_socket_subscriber_counts"}
    if verified_at is not None:
        local = _local_trace_time(verified_at)
        if local != verified_at:
            result["lastWakeVerifiedLocal"] = local
    return result


def command_session_status(args) -> None:
    result = session_status(args.alias, finish_registration=args.finish_registration)
    pairing = _read_pairing(args.alias)
    if pairing.get("client") == "claude" and pairing.get("session_id"):
        try:
            from claude_registration import status as receiver_status
            result["receiver"] = receiver_status(pairing["session_id"])
        except (ImportError, OSError, ValueError):
            result["receiver"] = {"state": "unconfirmed", "ready": False,
                                  "reason": "native_receiver_health_unavailable"}
    elif pairing.get("client") == "codex" and pairing.get("session_id"):
        try:
            from codex_registration import status as listener_status
            service_id = pairing["endpoint"].split("/services/", 1)[1].split("/", 1)[0].lower()
            result["receiver"] = listener_status(pairing["session_id"], service_id)
        except (ImportError, OSError, ValueError, KeyError, IndexError):
            result["receiver"] = {"state": "unconfirmed", "ready": False,
                                  "reason": "codex_listener_health_unavailable"}
    print(json.dumps(result, ensure_ascii=False))


def _session_row(event: dict, *, direct: bool, alias: str, service_id: str, cursor: int) -> dict | None:
    if not isinstance(event, dict) or type(event.get("sequence")) is not int or event["sequence"] < 1:
        raise ValueError("Invalid D-Bond session sequence")
    if event["sequence"] <= cursor:
        return None
    if direct:
        sender, target, source = event.get("from"), event.get("to"), event.get("id")
        text, parent = event.get("text"), event.get("in_reply_to")
        if (not valid_direct_source(source, sender)
                or not isinstance(sender, str) or (sender != "owner" and not SESSION_ID_RE.fullmatch(sender))
                or target != service_id or not isinstance(text, str) or not text.strip()
                or len(text.encode("utf-8")) > 65536
                or (parent is not None and (not isinstance(parent, str) or not EVENT_RE.fullmatch(parent)))):
            raise ValueError("Invalid addressed D-Bond direct message")
        return {"state": "direct_message", "alias": alias, "source_id": source,
                "sender_id": sender, "sequence": event["sequence"], "text": text,
                "in_reply_to": parent}
    if event.get("state") != "dot_push":
        return None
    source, text = event.get("id"), event.get("reply")
    if (not isinstance(source, str) or not re.fullmatch(r"@dot:[0-9]+", source)
            or not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 65536):
        raise ValueError("Invalid addressed D-Bond Dot push")
    return {"state": "dot_push", "alias": alias, "source_id": source,
            "sequence": event["sequence"], "text": text}


def _session_lane(alias: str, direct: bool, emit, stop: threading.Event, errors: list[str]) -> None:
    delay = 0.5
    while not stop.is_set():
        stream = None
        try:
            pairing = _read_pairing(alias)
            if not direct and pairing.get("destination") == "peer":
                return
            key = "direct_cursor" if direct else "cursor"
            cursor = pairing.get(key, 0)
            if type(cursor) is not int or cursor < 0:
                raise ValueError("Invalid saved D-Bond session cursor")
            service_id = pairing["endpoint"].split("/services/", 1)[1].split("/", 1)[0]
            stream_pairing = {**pairing, "endpoint": _direct_url(pairing)} if direct else pairing
            stream = Stream(stream_pairing, cursor)
            delay = 0.5
            while not stop.is_set():
                event = stream.next_event()
                if event is None:
                    raise ConnectionError("D-Bond session stream closed")
                if type(event.get("sequence")) is int and event["sequence"] <= cursor:
                    continue
                row = _session_row(event, direct=direct, alias=alias, service_id=service_id, cursor=cursor)
                if row is not None:
                    try:
                        emit(row)
                    except OSError as exc:
                        raise ValueError("Session output closed") from exc
                    if direct:
                        direct_delivery_receipt(pairing, event, adapter="cli-monitor")
                        cursor = pairing[key] = event["sequence"]
                    else:
                        pushes = pairing.setdefault("pushes", [])
                        if row["source_id"] not in pushes:
                            pushes.append(row["source_id"])
                        pairing["pushes"] = pushes[-256:]
                cursor = pairing[key] = event["sequence"]
                _save(alias, pairing)
        except (OSError, ConnectionError):
            if not stop.wait(delay):
                delay = min(20, delay * 2)
        except (ValueError, SystemExit, KeyError, IndexError) as exc:
            errors.append(f"{('direct' if direct else 'Dot')} session stream stopped: {exc}")
            stop.set()
        finally:
            if stream:
                stream.close()


def command_session_listen(args) -> None:
    pairing = _read_pairing(args.alias)
    if _migrate_claude_session_listener(args.alias, pairing):
        return
    direct_only = pairing.get("destination") == "peer"
    stop = threading.Event()
    threading.Thread(target=receipt_retry_loop, args=(pairing, stop), daemon=True).start()
    errors: list[str] = []
    output_lock = threading.Lock()

    def emit(row: dict) -> None:
        with output_lock:
            print(json.dumps(row, ensure_ascii=False, separators=(",", ":")), flush=True)

    lanes = [threading.Thread(target=_session_lane, args=(args.alias, True, emit, stop, errors), daemon=True)]
    if not direct_only:
        lanes.append(threading.Thread(target=_session_lane, args=(args.alias, False, emit, stop, errors), daemon=True))
    for lane in lanes:
        lane.start()
    try:
        while not stop.wait(0.25):
            pass
    except KeyboardInterrupt:
        stop.set()
    if errors:
        fail(errors[0])


def _migrate_claude_session_listener(alias: str, pairing: dict) -> bool:
    """Move an old Monitor rearm without racing a starting native consumer."""
    if pairing.get("client") != "claude":
        return False
    origin = _initiating_claude()
    if not origin or origin[0].lower() != pairing.get("session_id"):
        return False
    try:
        from claude_registration import attach
        listener = attach(alias, origin[0].lower(), pairing)
    except (ImportError, OSError, ValueError):
        return False
    if listener.get("ready") is not True and (listener.get("state") in ("unsupported", "waiting_host_bootstrap")
                                              or listener.get("reason") == "native_receiver_not_installed"):
        return False
    ready = listener.get("ready") is True
    receiver_state = listener.get("state", "unconfirmed")
    instruction = ("End this temporary Monitor; the native receiver is attached." if ready
                   else "Check receiver attachment with the verify command.")
    receiver = {key: value for key, value in listener.items() if key not in ("instruction", "verify_command")}
    print(json.dumps({"state": "native_receiver_attached" if ready else f"native_receiver_{receiver_state}",
                      "alias": alias, "receiver": receiver,
                      "verify_command": f"dbond session-status {alias}",
                      "instruction": instruction}), flush=True)
    return True


def command_trace(args) -> None:
    if not SESSION_ID_RE.fullmatch(args.connection_id):
        fail("Use the exact connection UUID shown by `dbond peers` or the D-Bond app.")
    try:
        if args.event_id:
            request = {"op": "trace", "id": args.connection_id, "eventID": args.event_id}
            if getattr(args, "direction", None):
                request["direction"] = args.direction
            if getattr(args, "counterpart_id", None):
                if not SESSION_ID_RE.fullmatch(args.counterpart_id):
                    fail("Use the exact counterpart connection UUID.")
                request["counterpartID"] = args.counterpart_id
            value = _owner_request(request)
        else:
            value = _owner_request({"op": "list_events", "id": args.connection_id, "limit": args.limit})
    except (OSError, ValueError) as exc:
        fail(f"Could not read the local event trace: {exc}")
    print(json.dumps(_format_trace(value), ensure_ascii=False))


def command_ask(args) -> None:
    pairing = _read_pairing(args.alias)
    payload = {"text": args.question}
    recipient = getattr(args, "to", None)
    if recipient is not None:
        if not SESSION_ID_RE.fullmatch(recipient):
            matches = [s for s in _peers(pairing) if s["name"].casefold() == recipient.casefold()]
            if len(matches) != 1:
                fail("Use the recipient's display name or ID from dbond peers.")
            recipient = matches[0]["id"]
        payload.update(kind="conversation.request", recipient_service=recipient.lower())
    event_id = args.event_id or str(uuid.uuid4())
    if not EVENT_RE.fullmatch(event_id) or event_id.startswith("@dot:"):
        fail("Event ID is invalid or reserved.")
    # Open and establish SSE first; replay cursor 0 catches early receipts after restarts.
    stream = Stream(pairing, 0)
    result = {"alias": args.alias, "event_id": event_id, "request_id": None, "state": "submitted"}
    request_file = _safe_dir() / ("request-" + event_id + ".json")
    _atomic_json(request_file, {"alias": args.alias, "event_id": event_id,
                                "created_at": time.time(), "state": "submitting"})
    try:
        status, receipt = _request(pairing, "POST", pairing["endpoint"],
                                   {"id": event_id, "payload": payload}, timeout=10)
        if status not in (200, 202) or receipt.get("id") != event_id:
            fail("D-Bond returned an invalid submission receipt.")
        if isinstance(receipt.get("reply"), str) and receipt["reply"].startswith("req_"):
            result["request_id"] = receipt["reply"]
        # POST/queued/stored is only the local receipt. Wait for explicit correlated ACK/reply.
        deadline = time.monotonic() + args.timeout
        terminal = None
        while True:
            event = stream.next_event(deadline)
            if event is None:
                break
            if event.get("id") != event_id:
                continue
            result["state"] = event.get("state", "unknown")
            result["sequence"] = event.get("sequence")
            if isinstance(event.get("reply"), str) and re.fullmatch(r"req_[0-9a-f]{32}", event["reply"]):
                result["request_id"] = event["reply"]
            if event.get("state") == "dot_acknowledged":
                result["ack"] = event.get("reply", "")
            if event.get("state") == "dot_replied":
                result["reply"] = event.get("reply", "")
                terminal = "dot_replied"
                break
            if event.get("state") in ("dot_cancelled", "dot_expired", "dot_route_blocked", "dot_quarantined"):
                terminal = event.get("state")
                result["detail"] = event.get("reply", "")
                break
        result["state"] = terminal or result["state"]
        request_record = {"alias": args.alias, "event_id": event_id, "request_id": result["request_id"],
                          "state": result["state"], "updated_at": time.time()}
        _atomic_json(request_file, request_record)
        print(json.dumps(result, ensure_ascii=False))
        if terminal is None:
            raise SystemExit(3)
    finally:
        stream.close()


def command_listen(args) -> None:
    pairing = _read_pairing(args.alias)
    cursor = pairing.get("cursor", 0)
    if not isinstance(cursor, int) or cursor < 0:
        fail("Pairing cursor is invalid.")
    delay = 0.5
    try:
        while True:
            stream = None
            try:
                stream = Stream(pairing, cursor)
                if not getattr(args,"push_only",False): print(json.dumps({"listening":args.alias,"after":cursor}),flush=True)
                while True:
                    event=stream.next_event()
                    if event is None: raise ConnectionError("D-Bond stream closed.")
                    delay=0.5
                    cursor=pairing["cursor"]=event["sequence"]
                    if event.get("state")=="dot_push" and re.fullmatch(r"@dot:[0-9]+",event.get("id","")):
                        pushes=pairing.setdefault("pushes",[])
                        if event["id"] not in pushes: pushes.append(event["id"]);pairing["pushes"]=pushes[-256:]
                        _save(args.alias,pairing)
                        print(json.dumps({"id":event["id"],"sequence":cursor,"state":"dot_push","text":event.get("reply","")},ensure_ascii=False),flush=True)
                    else: _save(args.alias,pairing)
            except (OSError,ConnectionError):
                # Transport reconnect only: no idle requests while the stream is alive.
                time.sleep(delay);delay=min(20,delay*2)
            finally:
                if stream: stream.close()
    except KeyboardInterrupt:
        print("Stopped listening.",file=sys.stderr)


def command_answer(args) -> None:
    pairing = _read_pairing(args.alias)
    dot_id = args.dot_id if args.dot_id.startswith("@dot:") else "@dot:" + args.dot_id
    if not re.fullmatch(r"@dot:[0-9]+", dot_id) or dot_id not in pairing.get("pushes", []):
        fail("Answer only a Dot push recorded by `dbond listen` for this pairing.")
    event_id = str(uuid.uuid4())
    stream = Stream(pairing, 0)
    result = {"alias": args.alias, "event_id": event_id, "in_reply_to": dot_id,
              "request_id": None, "state": "submitted"}
    try:
        status, receipt = _request(pairing, "POST", pairing["endpoint"], {
            "id": event_id,
            "payload": {"kind": "service.reply", "in_reply_to": dot_id, "text": args.text},
        })
        if status not in (200, 202) or receipt.get("id") != event_id:
            fail("D-Bond returned an invalid submission receipt.")
        deadline = time.monotonic() + args.timeout
        terminal = None
        while True:
            event = stream.next_event(deadline)
            if event is None:
                break
            if event.get("id") != event_id:
                continue
            result["state"] = event.get("state", "unknown")
            result["sequence"] = event.get("sequence")
            if isinstance(event.get("reply"), str) and re.fullmatch(r"req_[0-9a-f]{32}", event["reply"]):
                result["request_id"] = event["reply"]
            if event.get("state") == "dot_acknowledged":
                result["ack"] = event.get("reply", "")
            if event.get("state") == "dot_replied":
                result["reply"] = event.get("reply", "")
                terminal = "dot_replied"
                break
            if event.get("state") in ("dot_cancelled", "dot_expired", "dot_route_blocked", "dot_quarantined"):
                terminal = event.get("state")
                result["detail"] = event.get("reply", "")
                break
        result["state"] = terminal or result["state"]
        print(json.dumps(result, ensure_ascii=False))
        if terminal is None:
            raise SystemExit(3)
    finally:
        stream.close()


def wake_sequence(value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,19}", value) or not 1 <= int(value) <= 2**63 - 1:
        raise argparse.ArgumentTypeError("Use the broker sequence printed on the received wake check.")
    return int(value)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dbond", description="Send and receive events through a D-Bond service pairing.")
    sub = p.add_subparsers(dest="command", required=True)
    link = sub.add_parser("link", help="save a pairing without displaying its credential")
    link.add_argument("alias")
    link.add_argument("--file", help="JSON pairing file; use - or omit to read JSON from stdin")
    link.set_defaults(run=command_link)
    enroll = sub.add_parser("enroll", help="create a new service pairing through the running D-Bond app")
    enroll.add_argument("alias")
    enroll.add_argument("--name", help="display name shown in the D-Bond menu")
    enroll.add_argument("--chrome-id", help="allow only this Chrome extension origin to use this pairing in a browser")
    enroll.add_argument("--service", help="application or service, such as Build monitor")
    enroll.add_argument("--topic", help="chat title or workflow, such as Job intake")
    enroll.add_argument("--client", choices=("claude", "codex"), help="local client identity for the session")
    enroll.add_argument("--session-id", help="exact client session UUID; never inferred from nearby chats")
    enroll.add_argument("--title", help="title supplied by the host; preferred over local metadata")
    routes = enroll.add_mutually_exclusive_group()
    routes.add_argument("--destination", choices=("dot", "peer", "herald"), help="route to Dot, direct sessions, or local Herald notifications")
    routes.add_argument("--direct", action="store_true", help="create a direct-only pairing; existing Dot pairings can also use direct messaging")
    enroll.set_defaults(run=command_enroll)
    session_binding_cmd = sub.add_parser("session-binding", help="resolve one exact local host session to its private pairing without showing credentials")
    session_binding_cmd.add_argument("session_id", help="exact host session UUID")
    session_binding_cmd.add_argument("--client", required=True, choices=("claude", "codex"))
    session_binding_cmd.set_defaults(run=command_session_binding)
    ask = sub.add_parser("ask", help="submit a question and await a Dot ACK or reply")
    ask.add_argument("alias"); ask.add_argument("question")
    ask.add_argument("--timeout", type=int, default=120, help="seconds to await explicit Dot ACK/reply (default 120)")
    ask.add_argument("--event-id", help="reuse an ID when reconciling a prior submission")
    ask.add_argument("--to", metavar="NAME_OR_ID", help="ask Dot to deliver the message to a paired connection by name or ID")
    ask.set_defaults(run=command_ask)
    peers = sub.add_parser("peers", help="list connections by their D-Bond names and IDs")
    peers.add_argument("alias", help="your own paired alias")
    peers.set_defaults(run=lambda args: print(json.dumps(_peers(_read_pairing(args.alias)), ensure_ascii=False)))
    wake_ack = sub.add_parser("wake-ack", help="acknowledge this session’s received wake check by its broker sequence")
    wake_ack.add_argument("alias")
    wake_ack.add_argument("sequence", type=wake_sequence, help="sequence printed on the received wake check")
    def acknowledge_wake(args):
        from wake_registration import acknowledge
        print(json.dumps(acknowledge(args.alias, args.sequence)))
    wake_ack.set_defaults(run=acknowledge_wake)
    history = sub.add_parser("dot-inbox", help="read retained addressed Dot pushes in a sequence range without moving the listener")
    history.add_argument("alias")
    history.add_argument("--after", required=True, type=int)
    history.add_argument("--through", required=True, type=int)
    history.set_defaults(run=command_dot_inbox)
    direct_peers = sub.add_parser("direct-peers", help="list direct recipients (existing Dot and direct-only pairings)")
    direct_peers.add_argument("alias"); direct_peers.set_defaults(run=command_direct_peers)
    direct_send = sub.add_parser("direct-send", help="send directly to another paired service without Dot")
    direct_send.add_argument("alias"); direct_send.add_argument("to", help="exact recipient ID or display name"); direct_send.add_argument("text")
    direct_send.add_argument("--message-id", help="stable ID for safe retry after an interrupted POST")
    direct_send.add_argument("--wait-ms", type=int, default=10000, help="bounded automatic acknowledgment wait (default 10000; 0 for lookup only)")
    direct_send.set_defaults(run=command_direct_send)
    direct_answer = sub.add_parser("direct-answer", help="answer a direct message from the same paired sender")
    direct_answer.add_argument("alias"); direct_answer.add_argument("to", help="original sender ID"); direct_answer.add_argument("in_reply_to", help="original direct message ID"); direct_answer.add_argument("text")
    direct_answer.add_argument("--message-id", help="stable ID for safe retry after an interrupted POST")
    direct_answer.add_argument("--wait-ms", type=int, default=10000)
    direct_answer.set_defaults(run=command_direct_send)
    direct_ack = sub.add_parser("direct-ack", help="silently acknowledge an exact direct message accessed by this participant")
    direct_ack.add_argument("alias"); direct_ack.add_argument("sender_id"); direct_ack.add_argument("source_id")
    direct_ack.add_argument("sequence", type=int); direct_ack.set_defaults(run=command_direct_ack)
    direct_status = sub.add_parser("direct-status", help="read or briefly wait for authenticated peer delivery metadata")
    direct_status.add_argument("alias"); direct_status.add_argument("message_id"); direct_status.add_argument("sequence", type=int)
    direct_status.add_argument("--wait-ms", type=int, default=0); direct_status.set_defaults(run=command_direct_status)
    direct_receipts_cmd = sub.add_parser("direct-receipts", help="subscribe to sender-only delivery metadata; never route this output into agent chat")
    direct_receipts_cmd.add_argument("alias"); direct_receipts_cmd.add_argument("--after", type=int, default=0)
    direct_receipts_cmd.set_defaults(run=command_direct_receipts)
    direct_inbox = sub.add_parser("direct-inbox", help="read durable direct messages after a sequence")
    direct_inbox.add_argument("alias"); direct_inbox.add_argument("--after", type=int, default=None)
    direct_inbox.add_argument("--advance", action="store_true", help="advance the saved direct cursor")
    direct_inbox.set_defaults(run=command_direct_inbox)
    direct_listen = sub.add_parser("direct-listen", help="hold an event-driven direct-message stream and resume on reconnect")
    direct_listen.add_argument("alias"); direct_listen.set_defaults(run=command_direct_listen)
    session_listen = sub.add_parser("session-listen", help="hold both direct and addressed Dot streams for one paired host Monitor")
    session_listen.add_argument("alias"); session_listen.set_defaults(run=command_session_listen)
    session_status_cmd = sub.add_parser("session-status", help="check owner-observed direct and Dot stream subscriptions for one pairing")
    session_status_cmd.add_argument("alias")
    session_status_cmd.add_argument("--finish-registration", action="store_true", help="once explicitly requested after listener attach, schedule the delayed registration proof")
    session_status_cmd.set_defaults(run=command_session_status)
    trace = sub.add_parser("trace", help="show recent local event traces in this Mac's timezone")
    trace.add_argument("connection_id", help="exact connection UUID")
    trace.add_argument("event_id", nargs="?", help="optional exact event ID")
    trace.add_argument("--limit", type=int, choices=range(1, 101), default=20)
    trace.add_argument("--direction", choices=["local", "inbound", "outbound", "self"],
                       help="select one direction when an event ID appears more than once")
    trace.add_argument("--counterpart-id", help="exact other connection UUID for an ambiguous direct event ID")
    trace.set_defaults(run=command_trace)
    listen = sub.add_parser("listen", help="receive addressed Dot pushes via SSE")
    listen.add_argument("alias"); listen.add_argument("--push-only",action="store_true",help="Print only addressed pushes (for Claude Monitor)"); listen.set_defaults(run=command_listen)
    answer = sub.add_parser("answer", help="send a reply to a push previously recorded by listen")
    answer.add_argument("alias"); answer.add_argument("dot_id"); answer.add_argument("text")
    answer.add_argument("--timeout", type=int, default=120, help="seconds to await explicit Dot ACK/reply (default 120)")
    answer.set_defaults(run=command_answer)
    return p


def main() -> None:
    args = parser().parse_args()
    if getattr(args, "timeout", 1) <= 0:
        fail("Timeout must be a positive number of seconds.")
    args.run(args)


if __name__ == "__main__":
    main()
