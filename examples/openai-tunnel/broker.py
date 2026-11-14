"""The secure tunnel trial's paired sender, using the real Swift Hub and native_link."""
from contextlib import closing, contextmanager
import http.client
import json
import os
from pathlib import Path
import re
import selectors
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
SUPPORT = Path("Library/Application Support")
CONNECTOR = SUPPORT / "D-Bond/Dot Connector"
LEDGER = SUPPORT / "D-Bond Proof of Concept"
API_PORT = 18780
PAIR_NAME = "Tunnel probe / Round trip"


def private_read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r") as stream:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 16384):
            raise ValueError("Unexpected probe pairing file permissions")
        return stream.read()


def save(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", delete=False) as tmp:
        temp = Path(tmp.name)
        try:
            tmp.write(value)
            tmp.flush()
            os.fsync(tmp.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def _home(root):
    # Swift's guarded native test runner requires an actual short temporary HOME
    # for its sockaddr_un. Keep application data durable in the chosen state root.
    marker_path = root / "swift-home"
    try:
        name = private_read(marker_path)
    except FileNotFoundError:
        path = Path(tempfile.mkdtemp(dir="/tmp", prefix="dbp-"))
        try:
            with tempfile.NamedTemporaryFile(dir=root, mode="w", delete=False) as staged:
                staged.write(path.name)
                staged.flush()
                staged_name = staged.name
                try:
                    os.link(staged_name, marker_path)
                except FileExistsError:
                    path.rmdir()
                finally:
                    os.unlink(staged_name)
        except BaseException:
            if path.exists():
                path.rmdir()
            raise
        name = private_read(marker_path)
    if not re.fullmatch(r"dbp-[a-z0-9_]{8}", name):
        raise ValueError("Invalid probe runtime directory")
    home = Path("/tmp") / name
    home.mkdir(mode=0o700, exist_ok=True)
    info = home.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Probe runtime directory is unavailable")
    support = home / SUPPORT
    support.mkdir(mode=0o700, parents=True, exist_ok=True)
    if support.resolve() != home.resolve() / SUPPORT:
        raise ValueError("Unexpected probe support directory")
    return home


def database_path(root):
    return root / "swift-native" / "Dot Connector" / "bridge.sqlite3"


def prepare(root, settings):
    directory = database_path(root).parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    # native_link opens its watched directory, database and WAL with O_NOFOLLOW.
    # Link their grandparent to this trial's durable location.
    home = _home(root)
    native_parent = (home / CONNECTOR).parent
    try:
        native_parent.symlink_to(directory.parent.resolve(), target_is_directory=True)
    except FileExistsError:
        if not native_parent.is_symlink() or native_parent.resolve() != directory.parent.resolve():
            raise ValueError("Wrong native connector parent in probe")
    values = {"host": "127.0.0.1", "port": API_PORT, "api_key": settings.api_key,
              "reply_timeout_seconds": settings.reply_timeout_seconds}
    body = "".join(f"{key} = {json.dumps(value)}\n" for key, value in values.items())
    file = directory / "bridge.toml"
    if not file.exists() or private_read(file) != body:
        save(file, body)
    return database_path(root)


def _dbone(path, sql, args):
    with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        return db.execute(sql, args).fetchone()


def _read_line(proc):
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        if not selector.select(15):
            raise RuntimeError("Disposable Swift broker startup timed out")
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("Disposable Swift broker stopped during startup")
    return json.loads(line)


def _command(proc, command):
    proc.stdin.write(command + "\n")
    proc.stdin.flush()
    return _read_line(proc)


def _target(endpoint, path):
    url = urlsplit(endpoint)
    if (url.scheme != "http" or url.netloc != f"127.0.0.1:{url.port}" or url.port is None
            or url.path != path or url.query or url.fragment):
        raise ValueError("Expected the isolated Swift broker's local service endpoint")
    return url.port


def _stop(process):
    try:
        if process.poll() is None:
            process.stdin.write("quit\n")
            process.stdin.flush()
            process.wait(timeout=4)
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        # Observer helpers belong only to this isolated runner's process group.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=4)
        process.stdin.close()
        process.stdout.close()


@contextmanager
def start_broker(root):
    home = _home(root)
    binary = REPO / ".build/debug/dbond-probe"
    if not binary.is_file():
        raise RuntimeError("Build the dbond-probe product with swift build --product dbond-probe")
    # Preserve the ledger across restarts and macOS temporary file cleanup.
    # The actual directory (and its UNIX socket) must stay at the guarded temp path.
    ledger_dir = home / LEDGER
    ledger_dir.mkdir(mode=0o700, exist_ok=True)
    durable = root / "swift-ledger"
    durable.mkdir(mode=0o700, exist_ok=True)
    fixture_ledger = ledger_dir / "ledger.sqlite"
    # Link the containing directory of the database's WAL indirectly by resolving
    # the actual database filename through SQLite's native VFS symlink handling.
    durable_db = durable / "ledger.sqlite"
    durable_db.touch(mode=0o600, exist_ok=True)
    try:
        fixture_ledger.symlink_to(durable_db.resolve())
    except FileExistsError:
        if not fixture_ledger.is_symlink() or fixture_ledger.resolve() != durable_db.resolve():
            raise ValueError("Unexpected Swift ledger in probe runtime")
    # Native sender rejects symlinked attempt journals. Its real temporary HOME
    # retains the journal for the lifetime of the trial.
    env = {"PATH": os.defpath, "HOME": str(home), "DBOND_TEST_HOME": str(home),
           "DBOND_DOT_PYTHON": str(Path(sys.executable).resolve()),
           "DBOND_DOT_HELPER": str(REPO / "scripts/native_link.py")}
    process = subprocess.Popen([str(binary), str(ledger_dir), "0", "--native"], env=env, text=True,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    pair_file = root / "swift-pair.json"
    try:
        base = _read_line(process)["endpoint"]
        _target(base, "")
        services = _command(process, "list")["services"]
        if pair_file.exists():
            pair = json.loads(private_read(pair_file))
            if len(services) != 1 or services[0]["id"] != pair["id"] or services[0]["destination"] != "dot":
                raise RuntimeError("Stored Swift test pairing does not match the isolated ledger")
        else:
            if services:
                raise RuntimeError("Disposable Swift ledger contains an unexpected pairing")
            pair = _command(process, "json " + json.dumps({"op": "pair", "name": PAIR_NAME, "destination": "dot"}))
        pair["endpoint"] = base + "/v1/services/" + pair["id"] + "/events"
        pair["pid"] = process.pid
        save(pair_file, json.dumps(pair))
        yield
    finally:
        _stop(process)


def _endpoint(root, pair):
    # Confirm the saved address belongs to the current isolated broker generation
    # using its owner-only UNIX socket before sending the one-time pairing secret.
    path = _home(root) / LEDGER / "pair.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(4)
        client.connect(str(path))
        # Darwin <sys/un.h>: SOL_LOCAL=0, LOCAL_PEERPID=2.
        if client.getsockopt(0, 2) != pair["pid"]:
            raise RuntimeError("Probe broker changed while preparing the send")
        client.sendall(b'{"op":"list"}\n')
        with client.makefile("rb") as reader:
            result = json.loads(reader.readline(65536))
    rows = result["services"]
    match = next((s for s in rows if s["id"] == pair["id"] and s["destination"] == "dot"), None)
    if not match or match.get("paused"):
        raise RuntimeError("Private probe pairing is offline")
    path = "/v1/services/" + pair["id"] + "/events"
    return _target(pair["endpoint"], path), path


def submit(root, text, marker, *, context=None):
    if not re.fullmatch(r"DBOND-[a-f0-9]{16}", marker):
        raise ValueError("Expected a fresh synthetic probe ID")
    # After a restart the authenticated socket can accept connections just before
    # the server has published its new ephemeral port. Retry only this handshake.
    deadline = time.monotonic() + 5
    while True:
        try:
            pair = json.loads(private_read(root / "swift-pair.json"))
            port, path = _endpoint(root, pair)
            break
        except (FileNotFoundError, ConnectionRefusedError, RuntimeError):
            if time.monotonic() >= deadline:
                raise RuntimeError("Disposable paired broker is unavailable") from None
            time.sleep(.08)
    payload = {**(context or {}), "kind": "service.message", "text": text, "probe": marker}
    body = json.dumps({"id": marker, "payload": payload})
    with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=5)) as client:
        client.request("POST", path, body, {"Authorization": "Bearer " + pair["secret"],
                                           "Content-Type": "application/json"})
        if client.getresponse().status != 202:
            raise RuntimeError("Paired Swift service event was not accepted")
    # A bounded local test assertion waits for the asynchronous native sender;
    # the Swift service's actual message/receipt route uses its normal watcher.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        record = _dbone(database_path(root), """SELECT request_id FROM messages WHERE channel='d-bond'
            AND json_extract(metadata,'$.caller_id')='d-bond' AND json_extract(metadata,'$.conversation_id')=?
            AND json_extract(metadata,'$.dbond_event')=? AND json_extract(metadata,'$.data.probe')=?""",
                        (pair["id"], marker, marker))
        if record:
            return record[0]
        time.sleep(.08)
    raise TimeoutError("Swift broker did not persist its correlated native request")


def receipt(root, request_id, expected):
    pair = json.loads(private_read(root / "swift-pair.json"))
    correlated = _dbone(database_path(root), """SELECT json_extract(metadata,'$.dbond_event') FROM messages
        WHERE request_id=? AND json_extract(metadata,'$.conversation_id')=? AND json_extract(metadata,'$.data.probe')=?""",
                         (request_id, pair["id"], expected))
    if not correlated or correlated[0] != expected:
        return False
    saved = _dbone(root / "swift-ledger/ledger.sqlite", """SELECT
        max(json_extract(body,'$.state')='dot_acknowledged' AND instr(json_extract(body,'$.reply'),?)>0),
        max(json_extract(body,'$.state')='dot_replied' AND json_extract(body,'$.reply')=?)
        FROM receipts WHERE json_extract(body,'$.service')=? AND json_extract(body,'$.id')=?""",
                   (request_id, expected, pair["id"], correlated[0]))
    return saved == (1, 1)
