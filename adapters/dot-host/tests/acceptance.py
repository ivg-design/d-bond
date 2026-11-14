#!/usr/bin/env python3
"""Offline, synthetic packaged Dot path acceptance; never loads real user state.

Run with --app /path/to/D-Bond.app --probe /absolute/path/to/dbond-probe.
Without --app, use source modules and .build/dot-runtime. The probe must include
its guarded --native fixture seam. No production app or cloud service is started.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing, nullcontext
import hashlib
import hmac
import http.client
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import plistlib
import queue
import selectors
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import traceback
from urllib.parse import parse_qs, urlencode, urlsplit

ORIGIN = "https://offline.dbond.invalid"
CALLBACK = "https://offline-callback.dbond.invalid/events"
REDIRECT = "https://chatgpt.com/connector/oauth/offline-fixture"
SOP = {"version": 1, "rules": [{"id": "synthetic-owner-policy", "text": "Answer this synthetic fixture once and preserve its correlation."}]}
PAYLOAD = {"kind": "synthetic.question", "text": "What arrives through the offline fixture?", "synthetic": True}
REPLY = "SYNTHETIC: service payload and owner SOP were consumed."
PUSH = "SYNTHETIC: addressed follow-up for the same service."


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def endpoint(port, method, path, body=None, headers=None):
    if isinstance(body, dict):
        body = json.dumps(body, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", **(headers or {})}
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        content = response.read(1024 * 1024 + 1)
        require(len(content) <= 1024 * 1024, "Fixture response exceeds limit")
        return response.status, dict(response.getheaders()), content
    finally:
        connection.close()


def json_response(response):
    return json.loads(response[2])


def fresh_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(operation, description, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = operation()
            if value:
                return value
        except (OSError, ValueError, KeyError, sqlite3.Error, http.client.HTTPException):
            pass
        time.sleep(.05)
    raise AssertionError("Timed out: " + description)


def stop(process, graceful=False):
    if process is None or process.poll() is not None:
        return
    if graceful:
        try:
            process.stdin.write("quit\n")
            process.stdin.flush()
            process.wait(timeout=5)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        process.wait(timeout=5)


def child_mode(args):
    """Explicit harness-only port and callback substitutions in a real child process."""
    if args.child == "host":
        sys.path[:0] = [str(args.host_dir), str(args.host_dir / "vendor")]
        import host
        import core
        import httpx
        host.PORT = args.port  # no production default/configuration is changed
        original = core.create_app

        async def callback(url, body, headers):
            require(url == CALLBACK, "Fixture attempted an unexpected callback destination")
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                return await client.post(f"http://127.0.0.1:{args.callback_port}/events", content=body, headers=headers)

        def create(settings):
            return original(settings, bridge=core.DotInbox(settings, webhook_post=callback))

        core.create_app = create
        host.configure(args.state_dir, ORIGIN, REDIRECT)
        print(json.dumps({"fixture_configured": True}), flush=True)
        host.serve(args.state_dir)
    else:
        sys.path.insert(0, str(args.ingress_dir))
        import ingress
        with ThreadingHTTPServer(("127.0.0.1", args.port), ingress.handler(urlsplit(ORIGIN).netloc, args.upstream_port)) as server:
            print(json.dumps({"fixture_ingress_ready": True}), flush=True)
            server.serve_forever()


class Probe:
    def __init__(self, binary, directory, environment):
        self.process = subprocess.Popen([str(binary), str(directory), "0", "--native"], env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True)
        try:
            self.base = self.read()["endpoint"]
        except Exception:
            stop(self.process)
            raise
        self.port = urlsplit(self.base).port

    def read(self):
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            require(bool(selector.select(10)), "Release probe did not become ready")
        line = self.process.stdout.readline()
        require(bool(line), "Release probe exited before returning a result")
        return json.loads(line)

    def command(self, value):
        self.process.stdin.write("json " + json.dumps(value) + "\n")
        self.process.stdin.flush()
        return self.read()


class Callback:
    def __init__(self):
        self.key = os.urandom(32)
        self.secret = "whsec_" + base64.b64encode(self.key).decode()
        self.events = queue.Queue()
        self.deliveries = []
        self.failures = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                try:
                    require(self.path == "/events", "Unexpected synthetic callback path")
                    body = self.rfile.read(int(self.headers["Content-Length"]))
                    signed = self.headers["webhook-id"].encode() + b"." + self.headers["webhook-timestamp"].encode() + b"." + body
                    expected = "v1," + base64.b64encode(hmac.digest(outer.key, signed, hashlib.sha256)).decode()
                    require(any(hmac.compare_digest(expected, signature) for signature in self.headers["webhook-signature"].split()), "Synthetic webhook signature mismatch")
                    require(abs(time.time() - int(self.headers["webhook-timestamp"])) < 30, "Stale synthetic signing timestamp")
                    event = json.loads(body)
                    require(bool(self.headers.get("X-MCP-Subscription-Id")), "Missing subscription identity")
                    if event.get("type") == "verification":
                        result = {"challenge": event["challenge"]}
                    else:
                        require(event["eventId"] == self.headers["webhook-id"], "Event/header identity mismatch")
                        outer.deliveries.append(event["eventId"])
                        outer.events.put((time.monotonic(), event))
                        result = {}
                    encoded = json.dumps(result).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                except Exception as error:
                    outer.failures.append(type(error).__name__)
                    self.send_error(400)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


def oauth(port):
    verifier = "v" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    parameters = {"response_type": "code", "client_id": "d-bond", "redirect_uri": REDIRECT, "resource": ORIGIN + "/mcp",
                  "scope": "events:read messages:reply", "code_challenge": challenge, "code_challenge_method": "S256", "state": "synthetic"}
    authorization = endpoint(port, "GET", "/authorize?" + urlencode(parameters))
    require(authorization[0] == 302, "Synthetic OAuth authorization did not redirect")
    consent_url = urlsplit(authorization[1]["location"])
    consent = endpoint(port, "GET", consent_url.path + "?" + consent_url.query)
    require(consent[0] == 200, "Synthetic OAuth consent page unavailable")
    cookies = SimpleCookie()
    cookies.load(consent[1]["set-cookie"])
    return verifier, parse_qs(consent_url.query)["request_id"][0], "; ".join(f"{name}={value.value}" for name, value in cookies.items())


def complete_oauth(port, owner_key):
    verifier, request_id, cookie = oauth(port)
    consent = endpoint(port, "POST", "/consent", urlencode({"request_id": request_id, "owner_key": owner_key}).encode(),
        {"Origin": ORIGIN, "Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"})
    require(consent[0] == 302, "Synthetic OAuth owner consent failed")
    code = parse_qs(urlsplit(consent[1]["location"]).query)["code"][0]
    exchanged = endpoint(port, "POST", "/token", urlencode({"grant_type": "authorization_code", "code": code,
        "client_id": "d-bond", "redirect_uri": REDIRECT, "code_verifier": verifier, "resource": ORIGIN + "/mcp"}).encode(),
        {"Content-Type": "application/x-www-form-urlencoded"})
    require(exchanged[0] == 200, "Synthetic OAuth token exchange failed")
    return json_response(exchanged)["access_token"]


def rpc(port, token, method, params):
    response = endpoint(port, "POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": method,
        "params": {**params, "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                       "io.modelcontextprotocol/clientCapabilities": {}}}},
        {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2026-07-28",
         "Mcp-Method": method, **({"Mcp-Name": params["name"]} if "name" in params else {})})
    require(response[0] == 200, "Synthetic MCP request rejected: " + method)
    result = json_response(response)
    require("error" not in result, "Synthetic MCP protocol error: " + method)
    return result["result"]


def tool(port, token, name, arguments):
    result = rpc(port, token, "tools/call", {"name": name, "arguments": arguments})
    require(result.get("isError") is not True, "Synthetic MCP tool error: " + name)
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    return json.loads(next(item["text"] for item in result["content"] if item["type"] == "text"))


def database_row(database, query, parameters=()):
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute(query, parameters).fetchone()


def acceptance(args):
    root = Path(__file__).resolve().parents[3]
    if args.app:
        resources = args.app.resolve() / "Contents/Resources"
        runtime = resources / "dot-runtime/python/bin/python3.13"
        host_dir, ingress_dir, helper = resources / "dot-host", resources / "dot-ingress", resources / "native_link.py"
        with (args.app / "Contents/Info.plist").open("rb") as stream:
            version = plistlib.load(stream)["CFBundleShortVersionString"]
    else:
        runtime = root / ".build/dot-runtime/python/bin/python3.13"
        host_dir, ingress_dir, helper = root / "adapters/dot-host", root / "adapters/dot-ingress", root / "scripts/native_link.py"
        version = "source"
    probe_binary = args.probe.resolve()
    for path in (runtime, host_dir / "host.py", ingress_dir / "ingress.py", helper, probe_binary):
        require(path.is_file(), "Required offline acceptance component is missing: " + path.name)
    callback, probe, host_process, ingress_process = Callback(), None, None, None
    temporary_context = tempfile.TemporaryDirectory(prefix="dba-", dir="/tmp")
    try:
        # Keep Darwin's owner UNIX socket path below 104 bytes.
        # Cleanup happens after all child processes stop, including on failure.
        with nullcontext(temporary_context.name) as temporary:
            home = Path(temporary)
            state = home / "Library/Application Support/D-Bond/Dot Connector"
            ledger_dir = home / "Library/Application Support/D-Bond Proof of Concept"
            host_port, ingress_port = fresh_port(), fresh_port()
            environment = {**os.environ, "HOME": str(home), "DBOND_TEST_HOME": str(home),
                "DBOND_DOT_PYTHON": str(runtime), "DBOND_DOT_HELPER": str(helper), "PYTHONDONTWRITEBYTECODE": "1"}
            # Remove possible live bridge/runtime overrides from the child environment.
            for key in list(environment):
                if key.startswith("DOTS_BRIDGE_") or key in ("DBOND_DOTBRIDGE_DIR", "DBOND_SELECTED_DOT_ROUTE"):
                    environment.pop(key)

            def launch_host():
                process = subprocess.Popen([str(runtime), "-I", "-B", str(Path(__file__).resolve()), "--child", "host",
                    "--host-dir", str(host_dir), "--state-dir", str(state), "--port", str(host_port),
                    "--callback-port", str(callback.server.server_port)], env=environment,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
                try:
                    wait_for(lambda: endpoint(host_port, "GET", "/health")[0] == 200, "managed temporary host startup")
                except Exception:
                    stop(process)
                    raise
                return process

            host_process = launch_host()
            ingress_process = subprocess.Popen([str(runtime), "-I", "-B", str(Path(__file__).resolve()), "--child", "ingress",
                "--ingress-dir", str(ingress_dir), "--port", str(ingress_port), "--upstream-port", str(host_port)],
                env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            wait_for(lambda: endpoint(ingress_port, "GET", "/.well-known/oauth-authorization-server")[0] == 200, "restricted temporary ingress startup")
            require(endpoint(ingress_port, "GET", "/api/host-status")[0] == 404, "Ingress exposed owner API")
            with (state / "bridge.toml").open("rb") as stream:
                configuration = tomllib.load(stream)
            token = complete_oauth(ingress_port, configuration["owner_key"])
            rpc(ingress_port, token, "events/subscribe", {"name": "app.message.created", "arguments": {"channel": "d-bond"},
                "delivery": {"mode": "webhook", "url": CALLBACK, "secret": callback.secret}})
            probe = Probe(probe_binary, ledger_dir, environment)
            pair = probe.command({"op": "pair", "name": "Synthetic offline service", "destination": "dot", "sop": SOP})
            event_id = "synthetic-offline-request"
            began = time.monotonic()
            posted = endpoint(probe.port, "POST", urlsplit(pair["endpoint"]).path,
                {"id": event_id, "payload": PAYLOAD}, {"Authorization": "Bearer " + pair["secret"]})
            require(posted[0] == 202, "Disposable broker did not accept service event")
            callback_at, event = callback.events.get(timeout=20)
            native = event["data"]
            request_id = native["request_id"]
            require(native["channel"] == "d-bond", "Event reached an unexpected route")
            require(native["metadata"]["data"] == PAYLOAD, "Frozen service payload changed")
            require(native["metadata"]["dbond_owner_sop"] == SOP, "Frozen owner SOP changed")
            require(native["metadata"]["conversation_id"] == pair["id"], "Service correlation changed")
            ledger = ledger_dir / "ledger.sqlite"
            wait_for(lambda: json.loads(database_row(ledger, "SELECT body FROM events WHERE service=? AND id=?", (pair["id"], event_id))[0])["state"] == "dot_stored", "broker native storage receipt")
            stop(probe.process, graceful=True)
            probe = None
            stop(host_process)
            host_process = launch_host()
            probe = Probe(probe_binary, ledger_dir, environment)
            # The persisted OAuth token and request identity must work after both hosts restart.
            ack = tool(ingress_port, token, "acknowledge_request", {"request_id": request_id})
            ack_at = time.monotonic()
            require(ack["metadata"]["data"] == PAYLOAD and ack["metadata"]["dbond_owner_sop"] == SOP, "Simulated Dot did not read the intended context")
            again = tool(ingress_port, token, "acknowledge_request", {"request_id": request_id})
            require(again["already_started"] and again["started_at"] == ack["started_at"], "Repeated ACK changed start identity")
            tool(ingress_port, token, "reply_to_app", {"request_id": request_id, "text": REPLY})
            reply_at = time.monotonic()
            tool(ingress_port, token, "send_to_app", {"channel": "d-bond", "text": PUSH, "idempotency_key": "synthetic-offline-push",
                "metadata": {"recipient_caller_id": "d-bond", "recipient_conversation_id": pair["id"]}})

            def completion():
                original = database_row(ledger, "SELECT body FROM events WHERE service=? AND id=?", (pair["id"], event_id))
                if not original or json.loads(original[0])["state"] != "dot_replied":
                    return False
                pushed = database_row(ledger, "SELECT count(*) FROM events WHERE service=? AND id LIKE '@dot:%' AND json_extract(body,'$.state')='dot_push'", (pair["id"],))[0]
                return pushed == 1

            wait_for(completion, "real native observer correlated reply and addressed push")
            completed_at = time.monotonic()
            original = json.loads(database_row(ledger, "SELECT body FROM receipts WHERE json_extract(body,'$.service')=? AND json_extract(body,'$.id')=? AND json_extract(body,'$.state')='dot_replied'", (pair["id"], event_id))[0])
            require(original["reply"] == REPLY, "Correlated broker reply changed")
            push_record = json.loads(database_row(ledger, "SELECT body FROM receipts WHERE json_extract(body,'$.service')=? AND json_extract(body,'$.state')='dot_push'", (pair["id"],))[0])
            require(push_record["reply"] == PUSH, "Addressed broker push changed")
            duplicate = subprocess.run([str(runtime), "-I", "-B", str(helper), "send"], env={**environment,
                "DBOND_DOTBRIDGE_DIR": str(state), "DBOND_SELECTED_DOT_ROUTE": "owned"},
                input=json.dumps({"service": pair["id"], "id": event_id, "payload": PAYLOAD}),
                text=True, capture_output=True, timeout=10, check=False)
            require(duplicate.returncode == 0 and json.loads(duplicate.stdout)["state"] == "dot_replied", "Native helper did not reconcile the original reply")
            before_receipts = database_row(ledger, "SELECT count(*) FROM receipts")[0]
            stop(probe.process, graceful=True)
            probe = Probe(probe_binary, ledger_dir, environment)
            time.sleep(.3)  # bounded replay window, no live polling/monitor is installed
            require(database_row(ledger, "SELECT count(*) FROM receipts")[0] == before_receipts, "Broker restart duplicated receipts")
            require(database_row(state / "bridge.sqlite3", "SELECT count(*) FROM messages")[0] == 1, "Duplicate native message stored")
            require(len(callback.deliveries) == 1 and not callback.failures, "Duplicate or invalid synthetic callback")
            return {"result": "pass", "kind": "synthetic-offline-acceptance", "artifact_version": version,
                "checks": ["runtime and managed host child", "restricted ingress excludes owner API", "real OAuth consent and persisted grant",
                    "signed callback verification and delivery", "real native_link sender and WAL observer", "frozen payload and owner SOP",
                    "broker and host restart before ACK", "exact correlated reply and addressed push", "restart/retry no duplicate send"],
                "latency_seconds": {"submit_to_callback": round(callback_at - began, 3), "callback_to_ack_including_restart": round(ack_at - callback_at, 3),
                    "ack_to_reply": round(reply_at - ack_at, 3), "reply_to_broker_and_push": round(completed_at - reply_at, 3)},
                "native_message_count": 1, "signed_event_deliveries": 1,
                "artifact_hashes": {"native_link": hashlib.sha256(helper.read_bytes()).hexdigest(), "host_core": hashlib.sha256((host_dir / "core.py").read_bytes()).hexdigest()},
                "limitations": ["Simulated Dot model actions; no real Dot wake or account connection.",
                    "Harness-only ephemeral bind and callback routing replace production ports and HTTPS network delivery.",
                    "Uses the supplied release Hub probe, not GUI interaction or macOS installed-app lifecycle."]}
    finally:
        if probe:
            stop(probe.process, graceful=True)
        stop(host_process)
        stop(ingress_process)
        callback.close()
        temporary_context.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path)
    parser.add_argument("--probe", type=Path, default=Path(__file__).resolve().parents[3] / ".build/release/dbond-probe")
    parser.add_argument("--child", choices=("host", "ingress"), help=argparse.SUPPRESS)
    parser.add_argument("--host-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--ingress-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--state-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--upstream-port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--callback-port", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        child_mode(args)
        return
    try:
        print(json.dumps(acceptance(args), indent=2))
    except Exception as error:
        # Do not dump headers, subprocess stderr or configuration on failure.
        frame = traceback.extract_tb(error.__traceback__)[-1]
        print(json.dumps({"result": "fail", "kind": "synthetic-offline-acceptance", "reason": type(error).__name__,
                          "failure_location": Path(frame.filename).name + ":" + str(frame.lineno),
                          "detail": str(error) if isinstance(error, AssertionError) else "Fixture did not complete."}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
