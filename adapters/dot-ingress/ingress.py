#!/usr/bin/env python3
"""D-Bond's restricted public connector ingress; no owner/application API routes."""
from __future__ import annotations

import argparse
from collections import deque
import errno
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import platform
import re
import select
import signal
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
from urllib.parse import urlsplit

PORT = 18770
UPSTREAM_PORT = 18769
VERSION = "2026.11.0"
# GitHub's official release asset digests, verified 2026-11-14 via:
# https://api.github.com/repos/cloudflare/cloudflared/releases/tags/2026.11.0
ARCHIVES = {
    "arm64": ("cloudflared-darwin-arm64.tgz", "8c2f90277ef06a6080bb40b964d33f09f1188102f8798f2e344174b6cf41accf"),
    "x86_64": ("cloudflared-darwin-amd64.tgz", "8a59d51120cebbe902f0a8d6e1b7a7fd4f9a5bb4dcb3e0f05872483e75e7db6b"),
}
# Generated from the vendored PersonalOAuth/FastMCP app. Deliberately no
# /api, /register, callbacks, root, broad prefixes, or arbitrary upstream URL.
ROUTES = {
    "/.well-known/oauth-authorization-server": {"GET", "HEAD", "OPTIONS"},
    "/.well-known/oauth-protected-resource/mcp": {"GET", "HEAD", "OPTIONS"},
    "/authorize": {"GET", "HEAD", "POST"},
    "/token": {"POST", "OPTIONS"},
    "/revoke": {"POST", "OPTIONS"},
    "/consent": {"GET", "HEAD", "POST"},
    "/mcp": {"POST", "DELETE"},
}
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"}
MAX_BODY = 2 * 1024 * 1024


def hostname(value):
    value = value.strip().lower()
    if len(value) > 253 or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", value):
        raise ValueError("Enter a public DNS hostname without https:// or a path.")
    if value.endswith((".localhost", ".local", ".invalid", ".test", ".example")):
        raise ValueError("Enter your public DNS hostname.")
    return value


def permitted(method, target):
    # Reject ambiguous spellings BEFORE parsing: no encoded path, dot segments,
    # backslash, absolute-form URL, duplicate slash, fragment, or controls.
    if not target.startswith("/") or target.startswith("//") or any(ord(c) < 32 or ord(c) == 127 for c in target):
        return False
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or parts.fragment or any(c in parts.path for c in ("%", "\\")):
        return False
    return method in ROUTES.get(parts.path, set())


def safe_headers(headers):
    nominated = set()
    for value in headers.get_all("Connection", []):
        nominated.update(part.strip().lower() for part in value.split(","))
    return {key: value for key, value in headers.items()
            if key.lower() not in HOP | nominated
            and not key.lower().startswith(("x-forwarded-", "cf-", "proxy-"))
            and key.lower() not in {"forwarded", "x-real-ip"}}


def handler(public_host, upstream_port=UPSTREAM_PORT):
    class Proxy(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "D-Bond"

        def log_message(self, *_):
            pass  # OAuth queries, form content and tokens never enter logs.

        def parse_request(self):
            # BaseHTTPRequestHandler folds leading // into /. Preserve the
            # original target so the route boundary never accepts that rewrite.
            parts = self.raw_requestline.decode("iso-8859-1").rstrip("\r\n").split()
            self.original_target = parts[1] if len(parts) == 3 else ""
            return super().parse_request()

        def send_error(self, code, message=None, explain=None):
            self.close_connection = True
            body = b'{"error":"ingress_rejected"}'
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def forward(self):
            self.connection.settimeout(15)
            if self.original_target != self.path or not permitted(self.command, self.original_target):
                self.send_error(404)
                return
            if self.headers.get_all("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) > 1:
                self.send_error(400)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= MAX_BODY:
                    raise ValueError()
            except ValueError:
                self.send_error(413)
                return
            body = self.rfile.read(length)
            if len(body) != length:
                self.send_error(400)
                return
            headers = safe_headers(self.headers)
            headers.update({"Host": public_host, "Content-Length": str(length), "Connection": "close"})
            upstream = http.client.HTTPConnection("127.0.0.1", upstream_port, timeout=10)
            response_started = False
            try:
                upstream.connect()
                # A valid MCP tool call/stream may outlive a short HTTP timeout.
                # Connect is bounded; the authenticated app owns call lifetime.
                upstream.sock.settimeout(None)
                upstream.request(self.command, self.path, body=body, headers=headers)
                response = upstream.getresponse()
                self.send_response(response.status)
                response_started = True
                for key, value in response.getheaders():
                    if key.lower() not in HOP and not key.lower().startswith("x-forwarded-"):
                        self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                if self.command != "HEAD":
                    while True:
                        chunk = response.read1(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except (OSError, http.client.HTTPException):
                # Do not relay exception text, addresses, request contents or credentials.
                self.close_connection = True
                if not response_started:
                    self.send_error(502)
            finally:
                upstream.close()

        do_GET = do_HEAD = do_POST = do_DELETE = do_OPTIONS = forward
    return Proxy


def private_directory(path):
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Unsafe ingress support directory.")


def install_cloudflared(directory):
    if platform.system() != "Darwin" or platform.machine() not in ARCHIVES:
        raise ValueError("Cloudflare Tunnel supports Apple Silicon and Intel Macs.")
    private_directory(directory)
    name, expected = ARCHIVES[platform.machine()]
    executable = directory / ("cloudflared-" + VERSION + "-" + platform.machine())
    checksum = executable.with_suffix(".sha256")
    if executable.exists() and checksum.exists():
        for path in (executable, checksum):
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
                raise ValueError("Unsafe installed tunnel helper.")
        if hashlib.sha256(executable.read_bytes()).hexdigest() == checksum.read_text().strip():
            return executable
        raise ValueError("Installed tunnel helper changed. Remove it and set up again.")
    url = f"https://github.com/cloudflare/cloudflared/releases/download/{VERSION}/{name}"
    class HTTPSOnly(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, message, headers, destination):
            if urlsplit(destination).scheme != "https":
                raise ValueError("Tunnel download redirect must use HTTPS.")
            return super().redirect_request(request, fp, code, message, headers, destination)
    opener = urllib.request.build_opener(HTTPSOnly())
    with opener.open(url, timeout=60) as response:
        if urlsplit(response.url).scheme != "https":
            raise ValueError("Tunnel download must use HTTPS.")
        archive = response.read(64 * 1024 * 1024 + 1)
    if len(archive) > 64 * 1024 * 1024 or hashlib.sha256(archive).hexdigest() != expected:
        raise ValueError("Official tunnel archive checksum did not match.")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        members = [member for member in bundle.getmembers() if member.name in ("cloudflared", "./cloudflared")]
        if len(members) != 1 or not members[0].isfile() or members[0].size > 100 * 1024 * 1024:
            raise ValueError("Unexpected tunnel archive contents.")
        data = bundle.extractfile(members[0]).read()
    fd, staging = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(staging, 0o700)
        os.replace(staging, executable)
        checksum.write_text(hashlib.sha256(data).hexdigest())
        checksum.chmod(0o600)
    finally:
        if os.path.exists(staging):
            os.unlink(staging)
    return executable


def emit(state):
    print(json.dumps({"state": state}), flush=True)


class ParentWatch:
    """The app owns this helper; app exit reaps our proxy and tunnel child.

    No PID/port search and no termination of another app's process. Validate the
    actual startup parent before arming a one-shot kernel exit notification.
    """
    def __init__(self, on_exit, expected=None):
        expected = os.environ.get("DBOND_INGRESS_PARENT_PID") if expected is None else expected
        if not expected or not str(expected).isdigit() or int(expected) <= 1 or int(expected) != os.getppid():
            raise ValueError("Start ingress from D-Bond so its lifetime can be supervised.")
        self.queue = select.kqueue()
        self.closed = threading.Event()
        try:
            self.queue.control([select.kevent(int(expected), filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT, fflags=select.KQ_NOTE_EXIT)], 0, 0)
            if int(expected) != os.getppid():
                raise ValueError("The owning app exited during ingress startup.")
        except Exception:
            self.queue.close()
            raise
        def watch():
            try:
                if self.queue.control([], 1, None) and not self.closed.is_set():
                    on_exit()
            except OSError:
                if not self.closed.is_set():
                    on_exit()  # lost supervision must not leave public ingress running
        self.worker = threading.Thread(target=watch, daemon=True)
        self.worker.start()

    def close(self):
        self.closed.set()
        self.queue.close()


def terminal_tunnel_error(lines):
    text = b" ".join(lines).lower()
    return any(marker in text for marker in (b"invalid tunnel token", b"tunnel token is not valid",
        b"failed to parse tunnel token", b"provided tunnel token is not valid"))


def run(mode, public_host, directory, backoff=(1, 2, 4)):
    server = ThreadingHTTPServer(("127.0.0.1", PORT), handler(public_host))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stop = threading.Event()
    child = None
    def finish(*_):
        if stop.is_set():
            return
        stop.set()
        if child and child.poll() is None:
            child.terminate()
            owned = child
            def force_owned_exit():
                if owned.poll() is None:
                    owned.kill()
            deadline = threading.Timer(3, force_owned_exit)
            deadline.daemon = True
            deadline.start()
    signal.signal(signal.SIGTERM, finish)
    signal.signal(signal.SIGINT, finish)
    parent_watch = None
    try:
        parent_watch = ParentWatch(finish)
        emit("restricted_proxy_ready")
        if mode == "cloudflare":
            token = os.environ.pop("TUNNEL_TOKEN", "")
            if not token or len(token) > 16384 or any(c.isspace() for c in token):
                raise ValueError("Enter the tunnel token from your Cloudflare dashboard.")
            emit("preparing_official_tunnel")
            binary = install_cloudflared(directory)
            failures = 0
            while not stop.is_set():
                env = dict(os.environ, TUNNEL_TOKEN=token)
                child = subprocess.Popen([str(binary), "tunnel", "--no-autoupdate", "run"],
                    env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                tail = deque(maxlen=16)
                def drain(stream=child.stderr, recent=tail):
                    for line in iter(lambda: stream.readline(4096), b""):
                        recent.append(line)
                reader = threading.Thread(target=drain, daemon=True)
                reader.start()
                started = time.monotonic()
                emit("tunnel_process_started")
                child.wait()
                reader.join(1)
                child.stderr.close()
                if stop.is_set():
                    break
                if terminal_tunnel_error(tail):
                    raise ValueError("The tunnel token needs attention.")
                if time.monotonic() - started >= 60:
                    failures = 0
                if failures >= len(backoff):
                    raise RuntimeError("Tunnel recovery budget exhausted.")
                delay = backoff[failures]
                failures += 1
                emit("tunnel_reconnecting")
                stop.wait(delay)
        else:
            stop.wait()
    finally:
        finish()
        if parent_watch:
            parent_watch.close()
        server.shutdown()
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("https", "cloudflare"), required=True)
    parser.add_argument("--hostname", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        run(args.mode, hostname(args.hostname), Path.home() / "Library/Application Support/D-Bond/Dot Ingress")
    except ValueError:
        emit("ingress_config_error")
        raise SystemExit(64)
    except OSError as error:
        # A prior app-owned helper may still be releasing the port after app
        # exit. Retry within the app's bounded budget; never kill its occupant.
        emit("ingress_port_busy" if error.errno == errno.EADDRINUSE else "ingress_failed")
        raise SystemExit(75)
    except Exception:
        emit("ingress_failed")
        raise SystemExit(75)


if __name__ == "__main__":
    main()
