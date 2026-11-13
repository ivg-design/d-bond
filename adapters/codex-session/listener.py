#!/usr/bin/env python3
"""Opt-in, Mac-local D-Bond listener for one existing Codex Desktop chat.

Receives messages over SSE and submits the full text to the bound chat.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import plistlib
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
SUPPORT = HERE if (HERE / "dbond.py").is_file() else HERE.parent / "cli"
sys.path.insert(0, str(SUPPORT))
import dbond  # noqa: E402 - same reviewed CLI shipped beside this adapter
import owner  # noqa: E402 - same-user owner socket, no service bearer output
import ipc  # noqa: E402
import host_queue  # noqa: E402

STATE_DIR = Path.home() / "Library/Application Support/D-Bond Codex Listener"
# Darwin's AF_UNIX path limit is 104 bytes. Keep only the status socket in a
# short, owner-only directory; durable state remains in Application Support.
SOCKET_DIR = Path("/tmp") / f"dbond-codex-{os.getuid()}"
AGENTS_DIR = Path.home() / "Library/LaunchAgents"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
EXIT = threading.Event()


def identity(value: str) -> str:
    return str(uuid.UUID(value))


def private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Listener storage is not owner-only")


def check_private_dir(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Listener socket directory is not owner-only")


def private_json(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Listener state is not owner-only")
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("Invalid listener state")
    return value


def save_json(path: Path, value: dict) -> None:
    private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"), ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def paths(thread: str) -> dict[str, Path]:
    thread = identity(thread)
    return {"dir": STATE_DIR / thread, "binding": STATE_DIR / thread / "binding.json",
            "cursor_direct": STATE_DIR / thread / "cursor-direct.json",
            "cursor_dot": STATE_DIR / thread / "cursor-dot.json",
            "socket": SOCKET_DIR / f"s-{thread.replace('-', '')}.sock",
            "health": STATE_DIR / thread / "health.json",
            "lock": STATE_DIR / thread / "listener.lock", "receipts": STATE_DIR / thread / "receipts",
            "plist": AGENTS_DIR / f"com.ivg.dbond.codex.{thread.replace('-', '')}.plist"}


def label(thread: str) -> str:
    return f"com.ivg.dbond.codex.{identity(thread).replace('-', '')}"


def pairing_service(alias: str) -> tuple[dict, str]:
    try:
        pair = dbond._read_pairing(alias)
    except SystemExit as exc:
        raise ValueError("D-Bond pairing is unavailable") from exc
    parsed = urlsplit(pair["endpoint"])
    service = identity(parsed.path.split("/services/", 1)[1].split("/", 1)[0])
    return pair, service


def verify_pairing(alias: str, service: str) -> dict:
    pair, actual = pairing_service(alias)
    if actual != service:
        raise ValueError("Pairing was replaced; rebind from the intended Codex chat")
    services = owner.request({"op": "list"}, timeout=3).get("services", [])
    row = next((item for item in services if item.get("id") == service), None)
    if not row or row.get("paused") or row.get("destination") not in ("dot", "peer") or not row.get("name", "").startswith("Codex / "):
        raise ValueError("Bound service is not an active Codex connection")
    pair["_destination"] = row["destination"]
    return pair


def binding(thread: str) -> dict:
    item = private_json(paths(thread)["binding"])
    if item.get("thread_id") != identity(thread) or not isinstance(item.get("alias"), str) or not UUID.fullmatch(item.get("service_id", "")):
        raise ValueError("Invalid Codex listener binding")
    # Bindings created before host-aware routing were verified exclusively on local.
    item.setdefault("host_id", "local")
    if item["host_id"] not in ("local", "durable"):
        raise ValueError("Invalid Codex listener host")
    return item


def validate_frame(event: dict, service: str, after: int) -> dict:
    if not isinstance(event, dict) or type(event.get("sequence")) is not int or event["sequence"] <= after:
        raise ValueError("Invalid direct sequence")
    if not isinstance(event.get("id"), str) or not dbond.EVENT_RE.fullmatch(event["id"]):
        raise ValueError("Invalid direct event ID")
    if event.get("to") != service or not isinstance(event.get("text"), str) or not event["text"].strip():
        raise ValueError("Invalid addressed direct message")
    sender = event.get("from")
    if sender != "owner" and (not isinstance(sender, str) or not UUID.fullmatch(sender)):
        raise ValueError("Invalid direct sender")
    if len(event["text"].encode("utf-8")) > 65536:
        raise ValueError("Direct message exceeds size limit")
    return event


def cursor(path: Path) -> int:
    try:
        value = private_json(path).get("sequence")
    except FileNotFoundError:
        return 0
    if type(value) is not int or value < 0:
        raise ValueError("Invalid listener cursor")
    return value


def advance(path: Path, sequence: int) -> None:
    save_json(path, {"sequence": max(cursor(path), sequence)})


def receipt_file(directory: Path, event: dict, lane: str = "direct") -> Path:
    # Sequence is broker-generated and event ID remains in the receipt value.
    return directory / f"{lane}-{event['sequence']}.json"


def submit_message(cli: Path | None, thread: str, prompt: str, thread_owner: dict, *,
                   timeout: float = 20) -> dict:
    return ipc.deliver_to_owner(thread, prompt, thread_owner, timeout=timeout)


def acknowledge_host(bound: dict, event: dict) -> None:
    pair, service = pairing_service(bound["alias"])
    if event["to"] != service or bound["service_id"] != service:
        raise ValueError("Receipt pairing changed")
    result = dbond.direct_delivery_receipt(pair, event, adapter="codex-session",
                                           state="acknowledged", basis="host_acceptance")
    if result.get("state") == "receipt_queued":
        raise OSError("Broker receipt pending")


def report_host_unconfirmed(bound: dict, event: dict) -> None:
    pair, service = pairing_service(bound["alias"])
    if event["to"] != service or bound["service_id"] != service:
        raise ValueError("Receipt pairing changed")
    result = dbond.direct_delivery_receipt(pair, event, adapter="codex-session",
                                           state="unconfirmed", basis="host_result_unknown")
    if result.get("state") == "receipt_queued":
        raise OSError("Broker receipt pending")


def process_event(event: dict, bound: dict, folder: dict, lane: str = "direct",
                  submit=submit_message, discover=ipc.discover_owner,
                  cli: Path | None = None, retained: bool = False,
                  acknowledge=acknowledge_host,
                  report_unconfirmed=report_host_unconfirmed) -> str:
    """Persist the host's receipt before notifying the sender or advancing the stream."""
    cursor_path = folder["cursor_direct"] if lane == "direct" else folder["cursor_dot"]
    receipt = receipt_file(folder["receipts"], event, lane)
    try:
        record = private_json(receipt)
        if record.get("event_id") != event["id"]:
            raise ValueError("Broker sequence identity changed")
        if record["phase"] == "sending":
            record["phase"] = "ambiguous"
            if lane == "direct" and event["from"] != "owner":
                record["unconfirmed_pending"] = True
            save_json(receipt, record)
        if (not record.get("ack_pending") and not record.get("unconfirmed_pending")
                and record["phase"] != "ready"):
            advance(cursor_path, event["sequence"])
            return "already_attempted"
    except FileNotFoundError:
        record = {"event_id": event["id"], "sequence": event["sequence"], "lane": lane, "phase": "ready"}
        save_json(receipt, record)

    if record["phase"] == "ready":
        try:
            thread_owner = discover(bound["thread_id"], host_id=bound["host_id"], timeout=5)
        except (ipc.CodexWakeError, OSError):
            return "owner_offline"
        prompt = host_queue.inline_message(bound["alias"], lane, event, retained=retained,
                                           sender_name=event.get("sender_name"))
        save_json(receipt, {**record, "phase": "sending"})
        try:
            outcome = submit(cli, bound["thread_id"], prompt, thread_owner, timeout=20)
        except ipc.OwnerUnavailable:
            # Owner discovery failed before any host turn could be submitted.
            save_json(receipt, record)
            return "owner_offline"
        except Exception:
            outcome = {"phase": "ambiguous"}
        record.update(outcome)
        if (outcome["phase"] in ("steered", "started") and lane == "direct"
                and event["from"] != "owner"):
            record["ack_pending"] = True
        elif (outcome["phase"] == "ambiguous" and lane == "direct"
                and event["from"] != "owner"):
            record["unconfirmed_pending"] = True
        save_json(receipt, record)

    if record.get("ack_pending"):
        try:
            acknowledge(bound, event)
        except (OSError, ValueError, SystemExit, RuntimeError):
            # Retry only the idempotent receipt. The accepted host turn is already durable.
            return "receipt_pending"
        record["ack_pending"] = False
        save_json(receipt, record)
    if record.get("unconfirmed_pending"):
        try:
            report_unconfirmed(bound, event)
        except (OSError, ValueError, SystemExit, RuntimeError):
            # Retry metadata only. An unknown host result cannot be resubmitted.
            return "receipt_pending"
        record["unconfirmed_pending"] = False
        save_json(receipt, record)
    advance(cursor_path, event["sequence"])
    return record["phase"]


class StatusServer:
    def __init__(self, path: Path, thread: str, service: str, required: list[str],
                 health_path: Path | None = None):
        private_dir(path.parent)
        if len(os.fsencode(path)) >= 104:
            raise ValueError("Listener status socket path exceeds macOS limit")
        self.path = path
        self.health_path = health_path or path.parent / "health.json"
        self.required = set(required)
        self.open_streams: set[str] = set()
        self.value = {"state": "starting", "thread_id": thread, "service_id": service,
                      "transport": "offline", "host": "unknown", "wakeState": "unknown",
                      "ready": False}
        self.guard = threading.Lock()
        try:
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("Unsafe existing listener status socket")
            path.unlink()
        except FileNotFoundError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        os.chmod(path, 0o600)
        self.sock.listen(4)
        self._persist()
        threading.Thread(target=self._serve, daemon=True).start()

    def update(self, **changes):
        with self.guard:
            self.value.update(changes)
            self._derive()
            self._persist()

    def stream(self, lane: str, present: bool):
        with self.guard:
            if present:
                self.open_streams.add(lane)
            else:
                self.open_streams.discard(lane)
            self._derive()
            self._persist()

    def _persist(self):
        save_json(self.health_path, self.value)

    def _derive(self):
        complete = self.required.issubset(self.open_streams)
        self.value["transport"] = "subscribed" if complete else "partial" if self.open_streams else "offline"
        self.value["ready"] = complete and self.value.get("host") == "owner_available"
        if self.value["state"] not in ("blocked",):
            self.value["state"] = "listening" if self.value["ready"] else "degraded"

    def _serve(self):
        while not EXIT.is_set():
            try:
                peer, _ = self.sock.accept()
            except OSError:
                break
            with peer:
                try:
                    peer.settimeout(1)
                    with self.guard:
                        response = dict(self.value)
                    peer.sendall(json.dumps(response, separators=(",", ":")).encode() + b"\n")
                except OSError:
                    # A client can disconnect between accept and write. Keep
                    # the status socket alive for subsequent registration reads.
                    continue

    def close(self):
        self.sock.close()
        self.path.unlink(missing_ok=True)


def status(thread: str, *, refresh: bool = False) -> dict:
    folder = paths(thread)
    try:
        check_private_dir(folder["socket"].parent)
        info = folder["socket"].lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Unsafe listener status socket")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(str(folder["socket"]))
            data = client.recv(4096)
        value = json.loads(data)
        if isinstance(value, dict) and value.get("thread_id") == identity(thread):
            if refresh:
                return refreshed_status(thread, value)
            return value
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        pass
    try:
        last = private_json(folder["health"])
        if last.get("state") == "blocked" and last.get("thread_id") == identity(thread):
            return {**last, "ready": False, "transport": "offline"}
    except (FileNotFoundError, ValueError, OSError):
        pass
    return {"state": "stopped" if folder["binding"].exists() else "not_installed",
            "thread_id": identity(thread), "transport": "offline", "wakeState": "unknown",
            "ready": False}


def refreshed_status(thread: str, value: dict) -> dict:
    """Explicit read-only host and broker check; never runs on idle timers."""
    bound = binding(thread)
    value["host_id"] = bound["host_id"]
    try:
        ipc.discover_owner(thread, host_id=bound["host_id"], timeout=3)
        host = "owner_available"
    except (ipc.CodexWakeError, OSError):
        host = "owner_offline"
    value["host"] = host
    try:
        observed = dbond.session_status(bound["alias"])
        value["wakeState"] = observed.get("wakeState", "unknown")
        value["lastVerifiedAt"] = observed.get("lastWakeVerifiedAt")
        broker_ready = observed.get("state") == "subscriber_present"
    except (ValueError, SystemExit, OSError):
        value["wakeState"] = "unknown"
        broker_ready = False
    value["ready"] = (value.get("transport") == "subscribed" and
                      host == "owner_available" and broker_ready)
    if value.get("state") != "blocked":
        value["state"] = "listening" if value["ready"] else "degraded"
    return value


def snapshot_highwater(pair: dict, lane: str, after: int) -> int:
    """Find the first-attach boundary so retained messages can be labelled."""
    direct = lane == "direct"
    if direct:
        code, body = dbond._request(pair, "GET", dbond._direct_url(pair) + "/head", timeout=5)
        sequence = body.get("sequence") if isinstance(body, dict) else None
        if code != 200 or type(sequence) is not int or sequence < 0:
            raise ValueError("Could not read initial direct high-water")
        return max(after, sequence)
    endpoint = pair["endpoint"] + "/history"
    current = after
    for _ in range(128):
        query = "?after=" + str(current) + "&through=9223372036854775807"
        code, body = dbond._request(pair, "GET", endpoint + query, timeout=15)
        rows = body.get("messages") if isinstance(body, dict) else None
        if code != 200 or not isinstance(rows, list) or len(rows) > 32:
            raise ValueError("Could not read initial D-Bond backlog")
        last = body.get("lastSequence")
        more = body.get("hasMore")
        if type(last) is not int or last < current or type(more) is not bool:
            raise ValueError("Invalid initial Dot history cursor")
        if more and last == current:
            raise ValueError("Initial Dot history did not advance")
        current = last
        if not more:
            return current
    raise ValueError("Initial D-Bond backlog exceeds bounded review window")


def normalized(row: dict, service: str) -> dict:
    return {"id": row["source_id"], "sequence": row["sequence"],
            "from": row.get("sender_id", "dot"), "to": service, "text": row["text"],
            "in_reply_to": row.get("in_reply_to")}


def current_wake_unverified(service: str, source_id: str) -> bool:
    try:
        trace = owner.request({"op": "trace", "id": service, "eventID": source_id}, timeout=3).get("event")
        return isinstance(trace, dict) and trace.get("state") != "wake_verified"
    except (OSError, ValueError):
        return True  # preserve an uncertain challenge rather than discard it


def deliver(event: dict, bound: dict, folder: dict, lane: str, server: StatusServer,
            *, retained: bool = False) -> str:
    while not EXIT.is_set():
        outcome = process_event(event, bound, folder, lane, retained=retained)
        if outcome not in ("owner_offline", "receipt_pending"):
            break
        server.update(reason=outcome, host="owner_offline" if outcome == "owner_offline" else "owner_available",
                      state="degraded")
        if EXIT.wait(3):
            return outcome
    else:
        return "stopped"
    if outcome in ("steered", "started", "already_attempted"):
        server.update(state="listening", host="owner_available", reason=None)
    elif outcome == "ambiguous":
        server.update(host="unknown", reason="host_delivery_uncertain")
    try:
        observed = dbond.session_status(bound["alias"])
        server.update(wakeState=observed.get("wakeState", "unknown"),
                      lastVerifiedAt=observed.get("lastWakeVerifiedAt"))
    except (ValueError, SystemExit, OSError):
        pass
    return outcome


def lane_loop(lane: str, bound: dict, folder: dict, server: StatusServer,
              streams: dict[str, dbond.Stream], stream_lock: threading.Lock) -> None:
    direct = lane == "direct"
    cursor_path = folder["cursor_direct"] if direct else folder["cursor_dot"]
    delay = 0.5
    initial = not cursor_path.exists()
    while not EXIT.is_set():
        stream = None
        try:
            pair = verify_pairing(bound["alias"], bound["service_id"])
            key = "direct_cursor" if direct else "cursor"
            baseline = pair.get(key, 0)
            if type(baseline) is not int or baseline < 0:
                raise ValueError("Invalid saved D-Bond cursor")
            if initial:
                save_json(cursor_path, {"sequence": baseline})
            current = cursor(cursor_path)
            preexisting_max = snapshot_highwater(pair, lane, current) if initial else current
            initial = False
            endpoint = dbond._direct_url(pair) if direct else pair["endpoint"]
            try:
                stream = dbond.Stream({**pair, "endpoint": endpoint}, current)
            except SystemExit as exc:
                raise ValueError("Authenticated direct stream was rejected") from exc
            with stream_lock:
                streams[lane] = stream
            server.stream(lane, True)
            delay = 0.5
            while not EXIT.is_set():
                event = stream.next_event()
                if event is None:
                    raise ConnectionError("D-Bond stream closed")
                seq = event.get("sequence") if isinstance(event, dict) else None
                if type(seq) is not int or seq <= current:
                    raise ValueError("Invalid or repeated D-Bond stream sequence")
                row = dbond._session_row(event, direct=direct, alias=bound["alias"],
                                         service_id=bound["service_id"], cursor=current)
                current = seq
                # This exact paired recipient already handled the message explicitly.
                # Keep broker history intact; skip only Codex host reinjection.
                if direct and event.get("recipient_consumed") is True:
                    advance(cursor_path, seq)
                    continue
                if row is None:
                    advance(cursor_path, seq)
                    continue
                item = normalized(row, bound["service_id"])
                wake = direct and item["from"] == "owner" and item["id"].startswith("@wake:")
                retained = seq <= preexisting_max
                if wake and retained and not current_wake_unverified(bound["service_id"], item["id"]):
                    advance(cursor_path, seq)
                    continue
                if direct and item["from"] != "owner":
                    try:
                        peers = dbond._direct_peers(pair)
                        item["sender_name"] = next((peer["name"] for peer in peers
                                                    if peer.get("id") == item["from"]), None)
                    except (OSError, ValueError, SystemExit):
                        pass  # Stable sender ID remains available for addressing replies.
                deliver(item, bound, folder, lane, server, retained=retained and not wake)
        except (OSError, ConnectionError):
            server.stream(lane, False)
            if not EXIT.wait(delay):
                delay = min(20, delay * 2)
        except (ValueError, KeyError, IndexError, SystemExit) as exc:
            server.update(state="blocked", reason=type(exc).__name__)
            EXIT.set()
        finally:
            with stream_lock:
                streams.pop(lane, None)
            server.stream(lane, False)
            if stream is not None:
                stream.close()


def run(thread: str) -> dict:
    folder = paths(thread)
    private_dir(folder["dir"])
    lock_fd = os.open(folder["lock"], os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    bound = binding(thread)
    private_dir(folder["receipts"])
    pair = verify_pairing(bound["alias"], bound["service_id"])
    required = ["direct"] if pair["_destination"] == "peer" else ["direct", "dot"]
    server = StatusServer(folder["socket"], thread, bound["service_id"], required,
                          folder["health"])
    server.update(host_id=bound["host_id"])
    streams: dict[str, dbond.Stream] = {}
    stream_lock = threading.Lock()
    def stop(*_):
        EXIT.set()
        with stream_lock:
            active = list(streams.values())
        for stream in active:
            stream.close()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        try:
            ipc.discover_owner(thread, host_id=bound["host_id"], timeout=3)
            server.update(host="owner_available")
        except ipc.CodexWakeError:
            server.update(host="owner_offline")
        try:
            observed = dbond.session_status(bound["alias"])
            server.update(wakeState=observed.get("wakeState", "unknown"),
                          lastVerifiedAt=observed.get("lastWakeVerifiedAt"))
        except (ValueError, SystemExit, OSError):
            pass
        workers = [threading.Thread(target=lane_loop, args=(lane, bound, folder, server, streams, stream_lock),
                                    daemon=True) for lane in required]
        for worker in workers:
            worker.start()
        EXIT.wait()
        for worker in workers:
            worker.join(2)
        return {"state": "stopped"}
    finally:
        server.close()
        os.close(lock_fd)

def launchctl(*args: str) -> int:
    try:
        result = subprocess.run(["/bin/launchctl", *args], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=10, check=False)
        return result.returncode
    except (OSError, subprocess.TimeoutExpired):
        return -1


def install(thread: str, alias: str) -> dict:
    thread = identity(thread)
    if not dbond.ALIAS_RE.fullmatch(alias):
        raise ValueError("Invalid pairing alias")
    pair, service = pairing_service(alias)
    folder = paths(thread)
    prior = binding(thread) if folder["binding"].exists() else None
    if prior and (prior.get("thread_id"), prior.get("alias"), prior.get("service_id")) != (thread, alias, service):
        raise ValueError("Existing listener binding differs; stop and review it first")
    if not prior and (folder["cursor_direct"].exists() or folder["cursor_dot"].exists()
                      or folder["receipts"].exists()):
        raise ValueError("Prior listener audit exists; rebind requires owner review")
    if not prior and alias != f"codex-{thread}" and os.environ.get("CODEX_THREAD_ID") != thread:
        raise ValueError("Custom alias requires the exact active Codex chat")
    verify_pairing(alias, service)
    # Resolve both Desktop hosts on first attach. Reuse this route after restarts,
    # rediscovering the current owner client on that host for every delivery.
    try:
        thread_owner = ipc.discover_owner(thread, host_id=prior["host_id"] if prior else None, timeout=5)
    except ipc.CodexWakeError as exc:
        return {"state": "blocked", "reason": str(exc), "thread_id": thread, "ready": False}
    private_dir(folder["dir"])
    if not prior:
        save_json(folder["binding"], {"thread_id": thread, "alias": alias, "service_id": service,
                                      "host_id": thread_owner["host_id"]})
    AGENTS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    agent_info = AGENTS_DIR.lstat()
    if not stat.S_ISDIR(agent_info.st_mode) or agent_info.st_uid != os.getuid():
        raise ValueError("LaunchAgents directory is unsafe")
    program = [sys.executable, str(HERE / "listener.py"), "run", "--thread-id", thread]
    plist = {"Label": label(thread), "ProgramArguments": program, "RunAtLoad": True,
             "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 5,
             "EnvironmentVariables": {"DBOND_PAIRING_DIR": os.environ["DBOND_PAIRING_DIR"]}
                 if os.environ.get("DBOND_PAIRING_DIR") else {}}
    plist_data = plistlib.dumps(plist)
    if folder["plist"].exists() and folder["plist"].read_bytes() != plist_data:
        raise ValueError("Existing launch agent differs; review before changing it")
    if not folder["plist"].exists():
        fd = os.open(folder["plist"], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(plist_data)
            stream.flush()
            os.fsync(stream.fileno())
    current = status(thread)
    if current.get("ready") is True:
        return current
    if current["state"] in ("degraded", "listening", "blocked"):
        # A current listener owns the socket. Never kickstart over it merely
        # because one stream or the Desktop owner is briefly unavailable.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and current["state"] != "blocked" and not current.get("ready"):
            time.sleep(.15)
            current = status(thread)
        return current
    domain = f"gui/{os.getuid()}"
    if launchctl("bootstrap", domain, str(folder["plist"])) != 0:
        # May already be loaded but stopped; kickstart only this exact label.
        if launchctl("kickstart", "-k", f"{domain}/{label(thread)}") != 0:
            return {"state": "blocked", "reason": "launchd_start_failed", "thread_id": thread}
    deadline = time.monotonic() + 26
    while time.monotonic() < deadline:
        current = status(thread)
        if current.get("ready") is True or current["state"] == "blocked":
            return current
        time.sleep(.15)  # startup readiness handshake, not message polling
    return {"state": "degraded", "reason": "listener_not_ready", "thread_id": thread,
            "transport": "unknown", "wakeState": "unknown", "ready": False}


def stop(thread: str) -> dict:
    thread = identity(thread)
    folder = paths(thread)
    if not folder["plist"].exists():
        return {"state": "not_installed", "thread_id": thread}
    code = launchctl("bootout", f"gui/{os.getuid()}/{label(thread)}")
    current = status(thread)
    deadline = time.monotonic() + 3
    while current["state"] not in ("stopped", "not_installed") and time.monotonic() < deadline:
        time.sleep(.1)
        current = status(thread)
    if current["state"] not in ("stopped", "not_installed"):
        return {"state": "blocked", "thread_id": thread, "reason": "listener_still_running",
                "launchctl_exit": code}
    return {"state": "stopped", "thread_id": thread, "transport": "offline"}


def uninstall(thread: str) -> dict:
    thread = identity(thread)
    stopped = stop(thread)
    if stopped["state"] == "blocked":
        return stopped
    folder = paths(thread)
    folder["plist"].unlink(missing_ok=True)
    folder["binding"].unlink(missing_ok=True)
    # Event receipts and cursor remain as owner-only audit evidence.
    return {"state": "uninstalled", "thread_id": thread, "receipts_retained": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "run", "status", "stop", "uninstall"):
        item = sub.add_parser(name)
        item.add_argument("--thread-id", required=True)
        if name == "install":
            item.add_argument("--alias", required=True)
        item.add_argument("--json", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        thread = identity(args.thread_id)
        result = {"install": lambda: install(thread, args.alias), "run": lambda: run(thread),
                  "status": lambda: status(thread, refresh=True), "stop": lambda: stop(thread),
                  "uninstall": lambda: uninstall(thread)}[args.command]()
        print(json.dumps(result, separators=(",", ":")), flush=True)
    except Exception as exc:
        print(json.dumps({"state": "blocked", "reason": type(exc).__name__,
                          "detail": str(exc)[:240]}, separators=(",", ":")), flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
