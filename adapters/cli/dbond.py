#!/usr/bin/env python3
"""Universal local sender/receiver for a D-Bond service pairing."""
from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

APP_DIR = Path.home() / "Library/Application Support/D-Bond CLI"
OWNER_SOCKET = Path.home() / "Library/Application Support/D-Bond Proof of Concept/pair.sock"
ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
EVENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,119}$")
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
    current = home
    for part in relative.parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            fail("Pairing store path contains a symlink; refusing to use it.")
    current = APP_DIR
    if current.exists() and current.is_symlink():
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
    def __init__(self, pairing: dict, cursor: int):
        parsed = urllib.parse.urlsplit(pairing["endpoint"])
        self.sock = socket.create_connection((parsed.hostname, parsed.port or 80), timeout=10)
        self.reader = self.sock.makefile("rb")
        path = parsed.path + "/stream?after=" + str(cursor)
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
                    if event.get("sequence") != sequence:
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
            merged["cursor"] = max(int(existing.get("cursor", 0)), int(pairing.get("cursor", 0)))
            merged["direct_cursor"] = max(int(existing.get("direct_cursor", 0)), int(pairing.get("direct_cursor", 0)))
            merged["pushes"] = list(dict.fromkeys(existing.get("pushes", []) + pairing.get("pushes", [])))[-256:]
        _atomic_json(path, merged)
    finally:
        os.close(lock_fd)


def command_link(args) -> None:
    pairing = validate_pairing(_load_source(args.file))
    _save(args.alias, pairing)
    print(json.dumps({"linked": args.alias, "endpoint": pairing["endpoint"]}))


def command_enroll(args) -> None:
    path = _alias_path(args.alias)
    if path.exists() or path.is_symlink():
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
            if getattr(args, "direct", False):
                frame["destination"] = "peer"
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
        _atomic_json(path, pairing)
        print(json.dumps({"linked": args.alias, "endpoint": pairing["endpoint"]}))
        if getattr(args, "direct", False):
            print(f"To receive direct messages, run: dbond direct-listen {args.alias}", file=sys.stderr)
        elif getattr(args, "client", None) == "claude" or name.startswith("Claude / "):
            print(
                "To receive incoming messages, start this command with Monitor in the paired Claude chat: "
                f"dbond listen {args.alias} --push-only",
                file=sys.stderr,
            )
    finally:
        try:
            os.unlink(lock_path)
        except FileNotFoundError:
            pass


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
    custom_title = ai_title = desktop_title = None
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

    # Desktop session sidecars have an opaque filename, so inspect only the ID and
    # title fields needed for an exact cliSessionId match. Never emit unrelated data.
    metadata = home / "Library" / "Application Support" / "Claude" / "claude-code-sessions"
    if metadata.is_dir():
        for sidecar in metadata.glob("*/*/local_*.json"):
            try:
                with sidecar.open("r", encoding="utf-8") as src:
                    row = json.load(src)
                if isinstance(row, dict) and row.get("cliSessionId") == session_id:
                    candidate = row.get("title")
                    if isinstance(candidate, str):
                        desktop_title = _valid_title(candidate)
                    break
            except (OSError, UnicodeError, ValueError):
                continue
    return custom_title or ai_title or desktop_title


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


def command_direct_peers(args) -> None:
    print(json.dumps(_direct_peers(_read_pairing(args.alias)), ensure_ascii=False))


def command_direct_send(args) -> None:
    pairing = _read_pairing(args.alias)
    target = _direct_target(pairing, args.to)
    message_id = args.message_id or str(uuid.uuid4())
    if not EVENT_RE.fullmatch(message_id) or message_id.startswith("@dot:"):
        fail("Direct message ID is invalid or reserved.")
    body = {"id": message_id, "to": target, "text": args.text}
    if getattr(args, "in_reply_to", None):
        body["in_reply_to"] = args.in_reply_to
    status, receipt = _request(pairing, "POST", _direct_url(pairing), body)
    if status not in (200, 202) or receipt.get("id") != message_id:
        fail("D-Bond returned an invalid direct-message receipt.")
    print(json.dumps({"id": message_id, "to": target, "sequence": receipt.get("sequence"),
                      "duplicate": receipt.get("duplicate"), "state": "broker_recorded"}, ensure_ascii=False))


def command_direct_inbox(args) -> None:
    pairing = _read_pairing(args.alias)
    after = pairing.get("direct_cursor", 0) if args.after is None else args.after
    if type(after) is not int or after < 0:
        fail("Direct inbox cursor must be a nonnegative sequence.")
    status, body = _request(pairing, "GET", _direct_url(pairing) + "?after=" + str(after), timeout=15)
    if status != 200 or not isinstance(body.get("messages"), list):
        fail("D-Bond returned an invalid direct inbox.")
    rows = body["messages"]
    if args.advance and rows:
        pairing["direct_cursor"] = max(row["sequence"] for row in rows)
        _save(args.alias, pairing)
    print(json.dumps({"messages": rows, "after": after}, ensure_ascii=False))


def command_direct_listen(args) -> None:
    pairing = _read_pairing(args.alias)
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


def command_trace(args) -> None:
    if not SESSION_ID_RE.fullmatch(args.connection_id):
        fail("Use the exact connection UUID shown by `dbond peers` or the D-Bond app.")
    try:
        if args.event_id:
            value = _owner_request({"op": "trace", "id": args.connection_id, "eventID": args.event_id})
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
    enroll.add_argument("--service", help="application or service, such as Vollna")
    enroll.add_argument("--topic", help="chat title or workflow, such as Job intake")
    enroll.add_argument("--client", choices=("claude", "codex"), help="local client identity for the session")
    enroll.add_argument("--session-id", help="exact client session UUID; never inferred from nearby chats")
    enroll.add_argument("--title", help="title supplied by the host; preferred over local metadata")
    enroll.add_argument("--direct", action="store_true", help="create a direct-only pairing; existing Dot pairings can also use direct messaging")
    enroll.set_defaults(run=command_enroll)
    ask = sub.add_parser("ask", help="submit a question and await a Dot ACK or reply")
    ask.add_argument("alias"); ask.add_argument("question")
    ask.add_argument("--timeout", type=int, default=120, help="seconds to await explicit Dot ACK/reply (default 120)")
    ask.add_argument("--event-id", help="reuse an ID when reconciling a prior submission")
    ask.add_argument("--to", metavar="NAME_OR_ID", help="ask Dot to deliver the message to a paired connection by name or ID")
    ask.set_defaults(run=command_ask)
    peers = sub.add_parser("peers", help="list connections by their D-Bond names and IDs")
    peers.add_argument("alias", help="your own paired alias")
    peers.set_defaults(run=lambda args: print(json.dumps(_peers(_read_pairing(args.alias)), ensure_ascii=False)))
    direct_peers = sub.add_parser("direct-peers", help="list direct recipients (existing Dot and direct-only pairings)")
    direct_peers.add_argument("alias"); direct_peers.set_defaults(run=command_direct_peers)
    direct_send = sub.add_parser("direct-send", help="send directly to another paired service without Dot")
    direct_send.add_argument("alias"); direct_send.add_argument("to", help="exact recipient ID or display name"); direct_send.add_argument("text")
    direct_send.add_argument("--message-id", help="stable ID for safe retry after an interrupted POST")
    direct_send.set_defaults(run=command_direct_send)
    direct_answer = sub.add_parser("direct-answer", help="answer a direct message from the same paired sender")
    direct_answer.add_argument("alias"); direct_answer.add_argument("to", help="original sender ID"); direct_answer.add_argument("in_reply_to", help="original direct message ID"); direct_answer.add_argument("text")
    direct_answer.add_argument("--message-id", help="stable ID for safe retry after an interrupted POST")
    direct_answer.set_defaults(run=lambda args: command_direct_send(type("DirectArgs", (), {"alias":args.alias,"to":args.to,"text":args.text,"in_reply_to":args.in_reply_to,"message_id":args.message_id})()))
    direct_inbox = sub.add_parser("direct-inbox", help="read durable direct messages after a sequence")
    direct_inbox.add_argument("alias"); direct_inbox.add_argument("--after", type=int, default=None)
    direct_inbox.add_argument("--advance", action="store_true", help="advance the saved direct cursor")
    direct_inbox.set_defaults(run=command_direct_inbox)
    direct_listen = sub.add_parser("direct-listen", help="hold an event-driven direct-message stream and resume on reconnect")
    direct_listen.add_argument("alias"); direct_listen.set_defaults(run=command_direct_listen)
    trace = sub.add_parser("trace", help="show recent local event traces in this Mac's timezone")
    trace.add_argument("connection_id", help="exact connection UUID")
    trace.add_argument("event_id", nargs="?", help="optional exact event ID")
    trace.add_argument("--limit", type=int, choices=range(1, 101), default=20)
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
