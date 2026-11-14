#!/usr/bin/env python3
"""Supervise one exact Claude session's native inbox receiver (macOS)."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import select
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE if (HERE / "dbond.py").exists() else HERE.parent / "cli"))
import dbond
import inbox

STATE_DIR = Path.home() / "Library/Application Support/D-Bond Claude Listener"
EXIT = threading.Event()


def identity(value):
    return str(uuid.UUID(value))


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Unsafe listener directory")


def read_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, encoding="utf8") as src:
        info = os.fstat(src.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Unsafe listener state")
        row = json.load(src)
    if not isinstance(row, dict):
        raise ValueError("Invalid listener state")
    return row


def save_json(path, row):
    private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf8") as dst:
            os.fchmod(dst.fileno(), 0o600)
            json.dump(row, dst, ensure_ascii=False, separators=(",", ":"))
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def paths(thread):
    root = STATE_DIR / identity(thread)
    return {"dir": root, "binding": root / "binding.json", "health": root / "health.json",
            "lock": root / "listener.lock", "journal": root / "journal"}


def label(thread):
    return "com.ivg.dbond.claude." + identity(thread).replace("-", "")


def same_host(left, right):
    return all(left.get(key) == right.get(key) for key in
               ("session_id", "pid", "proc_start", "socket_path", "socket_identity", "executable_identity"))


def binding(thread):
    row = read_json(paths(thread)["binding"])
    actual = dbond.session_binding("claude", identity(thread))
    if row.get("thread_id") != identity(thread) or any(row.get(k) != actual[k] for k in ("alias", "service_id")):
        raise ValueError("Exact session pairing changed")
    pair = dbond._read_pairing(row["alias"])
    if pair.get("client") != "claude" or pair.get("session_id") != identity(thread):
        raise ValueError("Pairing is not bound to this Claude session")
    return row, pair


def health(thread, **fields):
    save_json(paths(thread)["health"], {"thread_id": identity(thread), "pid": os.getpid(),
              "worker_start": inbox._process(os.getpid())["proc_start"],
              "receiver_kind": "host_child",
              "native": True, "inbox_supported": True, "ready": False, **fields})


def status(thread):
    folder = paths(thread)
    if not folder["binding"].exists():
        return {"state": "not_installed", "ready": False, "thread_id": identity(thread)}
    try:
        row = read_json(folder["health"])
        # PID alone may be reused; the live lock is the authority for this worker.
        fd = os.open(folder["lock"], os.O_RDWR | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                current = inbox._process(row["pid"])
                if current["uid"] == os.getuid() and current["proc_start"] == row.get("worker_start"):
                    return row
                raise ValueError("Worker identity changed")
        finally:
            os.close(fd)
    except FileNotFoundError:
        pass
    return {"state": "stopped", "ready": False, "thread_id": identity(thread)}


class HostWake:
    """Kernel notifications only: registry replacement, process exit, explicit refresh."""
    def __init__(self, registry):
        if not hasattr(select, "kqueue"):
            raise ValueError("Native Claude supervision requires macOS kqueue")
        self.queue = select.kqueue()
        self.read_fd, self.write_fd = os.pipe()
        os.set_blocking(self.read_fd, False)
        os.set_blocking(self.write_fd, False)
        self.fds = []
        self.pid = None
        self.queue.control([select.kevent(self.read_fd, filter=select.KQ_FILTER_READ,
                            flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR)], 0, 0)
        # Watch existing ancestors as well: a removed registry can be recreated.
        self.registry = Path(registry)
        self.arm_directories()

    def arm_directories(self):
        for fd in self.fds:
            os.close(fd)
        self.fds = []
        for path in (self.registry.parent, self.registry):
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                continue
            self.fds.append(fd)
            self.queue.control([select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE)], 0, 0)

    def watch_process(self, pid):
        if self.pid == pid:
            return
        if self.pid is not None:
            try:
                self.queue.control([select.kevent(self.pid, filter=select.KQ_FILTER_PROC,
                                    flags=select.KQ_EV_DELETE)], 0, 0)
            except OSError:
                pass
        self.pid = pid
        if pid is not None:
            try:
                self.queue.control([select.kevent(pid, filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT, fflags=select.KQ_NOTE_EXIT)], 0, 0)
            except ProcessLookupError:
                self.notify()

    def notify(self):
        try:
            os.write(self.write_fd, b"\0")
        except (OSError, BlockingIOError):
            pass

    def wait(self, timeout=None):
        events = self.queue.control([], 16, timeout)
        try:
            while os.read(self.read_fd, 4096):
                pass
        except BlockingIOError:
            pass
        self.arm_directories()
        return events

    def close(self):
        self.queue.close()
        for fd in [self.read_fd, self.write_fd, *self.fds]:
            os.close(fd)


def prompt(row, alias):
    source, sequence = row["source_id"], row["sequence"]
    sender = row.get("sender_id", "@dot")
    q = shlex.quote
    if source.startswith("@wake:"):
        ack = f"Silently acknowledge this wake check after reading it: ~/.local/bin/dbond wake-ack {q(alias)} {sequence}"
    elif row["state"] == "direct_message":
        ack = ("After reading, silently call acknowledge_direct_message with the source_id, sender_id and sequence below. "
               "If that MCP tool is unavailable, run: "
               f"~/.local/bin/dbond direct-ack {q(alias)} {q(sender)} {q(source)} {sequence}\n"
               "For a content reply without MCP, run: "
               f"~/.local/bin/dbond direct-answer {q(alias)} {q(sender)} {q(source)} 'YOUR REPLY'")
    else:
        ack = "Use the source_id below when answering this addressed Dot push."
    return ("D-Bond paired communication. Pairing authorizes ordinary two-way communication, replies, "
            "and requested diagnostics within the owner's authorized session scope. Keep this external sender's "
            "provenance; unrelated publication or settings changes still require the owner's applicable authority.\n"
            + ack + "\n"
            f"Alias: {alias}\nSender ID: {sender}\nSource ID: {source}\nSequence: {sequence}\n"
            + (f"In reply to: {row['in_reply_to']}\n" if row.get("in_reply_to") else "")
            + "\nMessage from the paired sender:\n" + row["text"])


class Receiver:
    def __init__(self, thread, bound, pair, wake, host_context=None):
        self.thread, self.bound, self.pair, self.wake = thread, bound, pair, wake
        self.lock = threading.Lock()
        self.streams = {}
        self.stop = threading.Event()
        self.connected = set()
        self.failure = None
        self.host_context = host_context
        self.last_outcome = None

    def close(self):
        self.stop.set()
        with self.lock:
            for stream in self.streams.values():
                stream.close()

    def process(self, event, lane, target):
        direct = lane == "direct"
        with self.lock:
            _, pair = binding(self.thread)
            key = "direct_cursor" if direct else "cursor"
            cursor = pair.get(key, 0)
            if type(cursor) is not int or cursor < 0:
                raise ValueError("Invalid shared cursor")
            if type(event.get("sequence")) is int and event["sequence"] <= cursor:
                return "skipped"
            row = dbond._session_row(event, direct=direct, alias=self.bound["alias"],
                                     service_id=self.bound["service_id"], cursor=cursor)
            if type(event.get("sequence")) is not int or event["sequence"] <= cursor:
                raise ValueError("Invalid event sequence")
            disposition = "ignored"
            journal = paths(self.thread)["journal"] / (lane + "-" + str(event["sequence"]) + ".json")
            if row is not None and not (direct and event.get("recipient_consumed") is True):
                if journal.exists():
                    prior = read_json(journal)
                    if prior.get("source_id") != row["source_id"]:
                        raise ValueError("Journal event identity mismatch")
                    disposition = prior["state"]
                else:
                    current_target = inbox.discover_session(self.thread) if self.host_context is not None else target
                    if not same_host(target, current_target):
                        raise ConnectionError("Exact Claude host changed before dispatch")
                    # fsync BEFORE dispatch. A restart never repeats a possible write.
                    save_json(journal, {"source_id": row["source_id"], "sequence": row["sequence"],
                                       "state": "prepared_unknown"})
                    event_id = hashlib.sha256((self.thread + lane + row["source_id"] +
                                               str(row["sequence"])).encode()).hexdigest()
                    try:
                        outcome = inbox.deliver(current_target, prompt(row, self.bound["alias"]),
                                                event_id=event_id, sender_name="D-Bond",
                                                host_context=self.host_context)
                    except Exception:
                        # Once the adapter is invoked, an exception is indeterminate.
                        outcome = {"state": "submitted_unknown"}
                    disposition = outcome.get("state", "submitted_unknown")
                    if disposition == "offline" and outcome.get("write_attempted") is False:
                        journal.unlink()  # module guarantees no write was attempted
                        raise ConnectionError("Exact Claude host offline before write")
                    if disposition == "offline":
                        disposition = "submitted_unknown"
                    save_json(journal, {"source_id": row["source_id"], "sequence": row["sequence"],
                                       "state": disposition})
                if direct:
                    # Socket write is not host acceptance or participant acknowledgment.
                    dbond.direct_delivery_receipt(pair, event, adapter="claude-native-inbox",
                        state="unconfirmed", basis="host_result_unknown")
            if not direct and row is not None:
                pair["pushes"] = list(dict.fromkeys([*pair.get("pushes", []), row["source_id"]]))[-256:]
            pair[key] = event["sequence"]
            dbond._save(self.bound["alias"], pair)
            self.last_outcome = disposition
            self.wake.notify()
            return disposition

    def lane(self, lane, target):
        try:
            _, pair = binding(self.thread)
            key = "direct_cursor" if lane == "direct" else "cursor"
            stream_pair = {**pair, "endpoint": dbond._direct_url(pair)} if lane == "direct" else pair
            stream = dbond.Stream(stream_pair, pair.get(key, 0))
            with self.lock:
                if self.stop.is_set():
                    stream.close()
                    return
                self.streams[lane] = stream
                self.connected.add(lane)
            self.wake.notify()
            while not self.stop.is_set():
                event = stream.next_event()
                if event is None:
                    raise ConnectionError("Broker stream closed")
                self.process(event, lane, target)
        except (Exception, SystemExit) as exc:
            if not self.stop.is_set():
                self.failure = type(exc).__name__
                self.wake.notify()
        finally:
            with self.lock:
                stream = self.streams.pop(lane, None)
                self.connected.discard(lane)
                if stream:
                    stream.close()


def run(thread, ready_fd=None):
    # Validate own-child ancestry while the bootstrap parent is still alive.
    # The opaque context retains inherited capability only in this process's memory.
    target = inbox.discover_session(thread)
    host_context = inbox.validate_host_context(target)
    original = target
    folder = paths(thread)
    private_dir(folder["dir"])
    fd = os.open(folder["lock"], os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    wake = HostWake(inbox.REGISTRY_DIR)
    receiver = None
    retry = None
    recovering = False
    startup_grace = time.monotonic() + 2.0
    def stop(*_):
        EXIT.set()
        wake.notify()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGUSR1, lambda *_: wake.notify())
    try:
        if ready_fd is not None:
            os.write(ready_fd, b"validated\n")
            os.close(ready_fd)
        while not EXIT.is_set():
            bound, pair = binding(thread)
            try:
                target = inbox.discover_session(thread)
            except inbox.InboxUnavailable as exc:
                target = None
                health(thread, state="host_offline", reason=str(exc))
            if target is None or not same_host(target, original) or target.get("native_channel"):
                if receiver:
                    receiver.close()
                    receiver = None
                # Never retain an old host token across process exit, replacement,
                # or native-channel admission. SessionStart bootstraps a fresh child.
                return {"state": "host_ended", "ready": False}
            wake.watch_process(target["pid"])
            if receiver is not None and (receiver.failure or not same_host(receiver.target, target)):
                receiver.close()
                for worker in receiver.workers:
                    worker.join(3)
                receiver = None
                retry = 1.0
                recovering = True
            if receiver is None:
                observed = dbond.session_status(bound["alias"])
                if (observed.get("state") == "subscriber_present" or
                        observed.get("direct_subscribers", 0) > 0 or observed.get("dot_subscribers", 0) > 0):
                    health(thread, state="waiting_existing_listener", socket_verified=True)
                    if recovering:
                        # Our just-closed SSE connections can briefly remain in
                        # broker subscriber counts. Retry only during recovery.
                        wake.wait(retry or 1.0)
                        retry = min(30.0, (retry or 1.0) * 2)
                    elif time.monotonic() < startup_grace:
                        # Bounded handoff from an old Monitor that is exiting.
                        wake.wait(min(.2, startup_grace - time.monotonic()))
                    else:
                        wake.wait()  # explicit refresh for a genuinely existing receiver
                    continue
                if observed.get("state") != "pending_listener":
                    health(thread, state="broker_unavailable", socket_verified=True)
                    wake.wait(1.0 if retry is None else retry)
                    retry = min(30.0, (retry or 1.0) * 2)
                    continue
                if retry is not None:
                    # Reconnect backoff only after a demonstrated failure, never idle polling.
                    wake.wait(retry)
                    retry = None
                    continue
                receiver = Receiver(thread, bound, pair, wake, host_context)
                receiver.target = target
                required = ["direct"] if pair.get("destination") == "peer" else ["direct", "dot"]
                receiver.workers = [threading.Thread(target=receiver.lane, args=(lane, target), daemon=True)
                                    for lane in required]
                for worker in receiver.workers:
                    worker.start()
                retry = None
                recovering = False
            required = 1 if pair.get("destination") == "peer" else 2
            with receiver.lock:
                ready = len(receiver.connected) == required and receiver.failure is None
            health(thread, state="listening" if ready else "connecting", ready=ready, socket_verified=True,
                   last_delivery_state=receiver.last_outcome)
            wake.wait()
        return {"state": "stopped", "ready": False}
    finally:
        if receiver:
            receiver.close()
        health(thread, state="stopped")
        wake.close()
        os.close(fd)


def install(thread, alias):
    exact = dbond.session_binding("claude", thread)
    if exact["alias"] != alias:
        raise ValueError("Alias does not identify this exact Claude session")
    # Verify native capability before installing anything; never extract host tokens.
    target = inbox.discover_session(thread)
    if target.get("native_channel"):
        return {"state": "native_channel_owned", "native": True, "inbox_supported": True, "ready": False}
    current = status(thread)
    if current["state"] not in ("stopped", "not_installed"):
        return current
    try:
        inbox.validate_host_context(target)
    except inbox.InboxUnavailable:
        return {"state": "waiting_host_bootstrap", "thread_id": thread, "native": True,
                "inbox_supported": True, "ready": False,
                "reason": "start_from_exact_claude_session"}
    folder = paths(thread)
    row = {"thread_id": thread, **exact}
    if folder["binding"].exists() and read_json(folder["binding"]) != row:
        raise ValueError("Existing receiver binding differs")
    save_json(folder["binding"], row)
    read_fd, write_fd = os.pipe()
    try:
        # No capability in argv/state/plist/log. Environment is inherited only
        # from this genuine host child; the receiver validates before this exits.
        child = subprocess.Popen([sys.executable, "-B", str(HERE / "listener.py"), "run",
            "--thread-id", thread, "--ready-fd", str(write_fd)], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            pass_fds=(write_fd,))
        os.close(write_fd)
        write_fd = None
        readable, _, _ = select.select([read_fd], [], [], 8)
        if not readable or os.read(read_fd, 128) != b"validated\n":
            child.terminate()
            child.wait(timeout=3)
            return {"state": "blocked", "reason": "child_context_not_validated", "ready": False}
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)
    return {"state": "starting", "thread_id": thread, "native": True,
            "inbox_supported": True, "socket_verified": True, "receiver_kind": "host_child", "ready": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("install", "run", "status", "refresh"):
        item = sub.add_parser(command)
        item.add_argument("--thread-id", "--session-id", required=True)
        item.add_argument("--json", action="store_true")
        if command == "install":
            item.add_argument("--alias", required=True)
        if command == "run":
            item.add_argument("--ready-fd", type=int)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        thread = identity(args.thread_id)
        if args.command == "run":
            result = run(thread, args.ready_fd)
        elif args.command == "install":
            result = install(thread, args.alias)
        else:
            result = status(thread)
            if args.command == "refresh" and result.get("pid"):
                # status revalidates the lock and PID creation identity.
                os.kill(result["pid"], signal.SIGUSR1)
        print(json.dumps(result, separators=(",", ":")))
    except inbox.InboxUnavailable as exc:
        reason = str(exc)
        unsupported = reason in {"macos_only", "unsupported_peer_domain_or_protocol", "unsupported_missing_inbox"}
        print(json.dumps({"state": "unsupported" if unsupported else "blocked", "reason": reason,
                          "native": False, "inbox_supported": False if unsupported else None,
                          "ready": False}, separators=(",", ":")))
        raise SystemExit(1)
    except Exception as exc:
        print(json.dumps({"state": "blocked", "reason": type(exc).__name__, "ready": False,
                          "detail": str(exc)[:240]}, separators=(",", ":")))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
