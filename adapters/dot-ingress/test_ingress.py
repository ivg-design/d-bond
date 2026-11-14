"""Disposable proxy fixtures; never bind production ports or create a tunnel."""
import contextlib
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

import ingress


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.received = []
        received = self.received
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def answer(self):
                received.append((self.command, self.path, dict(self.headers),
                                 self.rfile.read(int(self.headers.get("Content-Length", "0")))))
                body = b'{"issuer":"https://connector.example.org"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Set-Cookie", "oauth_csrf=fixture; Path=/consent; Secure; HttpOnly")
                self.end_headers()
                if self.command != "HEAD": self.wfile.write(body)
            do_GET = do_HEAD = do_POST = do_DELETE = do_OPTIONS = answer
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.proxy = ThreadingHTTPServer(("127.0.0.1", 0), ingress.handler("connector.example.org", self.upstream.server_port))
        for server in (self.upstream, self.proxy):
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(self.close)

    def close(self):
        for server in (self.proxy, self.upstream):
            server.shutdown()
            server.server_close()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=2)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.getheaders(), response.read()
        finally:
            connection.close()

    def test_authoritative_route_methods_only(self):
        for path, methods in ingress.ROUTES.items():
            for method in methods:
                self.assertEqual(self.request(method, path)[0], 200, (method, path))
        for method, path in (("GET", "/mcp"), ("GET", "/token"), ("DELETE", "/consent"), ("POST", "/.well-known/oauth-authorization-server")):
            self.assertEqual(self.request(method, path)[0], 404)

    def test_private_routes_and_ambiguous_paths_never_reach_upstream(self):
        bad = ["/", "/api", "/api/channels", "/api/owner", "/register", "/owner", "/mcp/../api",
               "/mcp/", "//mcp", "/%6dcp", "/mcp%2f..%2fapi", "/mcp%252f..%252fapi", "/mcp\\..\\api",
               "/.well-known/oauth-protected-resource", "/authorize/../api", "/consent#fragment"]
        for path in bad:
            self.assertEqual(self.request("POST", path)[0], 404, path)
        self.assertEqual(self.received, [])

    def test_oauth_consent_form_cookie_origin_and_query_survive(self):
        body = b"request_id=fixture&owner_key=private-fixture&csrf=encoded%2Bvalue"
        status, headers, _ = self.request("POST", "/consent?flow=fixture", body, {
            "Content-Type": "application/x-www-form-urlencoded", "Origin": "https://connector.example.org",
            "Cookie": "oauth_csrf=fixture", "X-Forwarded-Host": "attacker.invalid",
            "X-Forwarded-Proto": "http", "Forwarded": "host=attacker.invalid", "CF-Connecting-IP": "127.0.0.1"})
        self.assertEqual(status, 200)
        method, path, forwarded, actual_body = self.received[-1]
        self.assertEqual(actual_body, body)
        self.assertEqual(path, "/consent?flow=fixture")
        self.assertEqual(forwarded["Content-Type"], "application/x-www-form-urlencoded")
        self.assertEqual(forwarded["Origin"], "https://connector.example.org")
        self.assertEqual(forwarded["Cookie"], "oauth_csrf=fixture")
        self.assertEqual(forwarded["Host"], "connector.example.org")
        self.assertFalse(any(key.lower().startswith(("x-forwarded-", "cf-")) or key.lower() == "forwarded" for key in forwarded))
        self.assertTrue(any(key.lower() == "set-cookie" for key, _ in headers))

    def test_token_form_and_mcp_authorization_survive(self):
        self.request("POST", "/token", b"grant_type=authorization_code&code=fixture", {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertIn(b"grant_type=authorization_code", self.received[-1][3])
        self.request("POST", "/mcp", b'{}', {"Authorization": "Bearer fixture", "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18"})
        self.assertEqual(self.received[-1][2]["Authorization"], "Bearer fixture")
        self.assertEqual(self.received[-1][2]["MCP-Protocol-Version"], "2025-06-18")

    def test_transfer_encoding_is_never_forwarded(self):
        self.assertEqual(self.request("POST", "/mcp", b"{}", {"Transfer-Encoding": "chunked"})[0], 400)
        self.assertEqual(self.received, [])


class DownloadTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(select, "kqueue"), "macOS parent lifetime notification")
    def test_app_crash_releases_only_its_fixture_proxy_port(self):
        # An app stub owns the actual ingress helper. Both run in isolated HOME;
        # override only the bind port to 0 so production18769/18770 are untouched.
        child_code = '''import json,os,sys
from pathlib import Path
sys.path.insert(0,os.environ['QA_INGRESS_MODULE'])
import ingress
original=ingress.ThreadingHTTPServer
created=[]
def server(address,handler):
    result=original(('127.0.0.1',0),handler);created.append(result);return result
ingress.ThreadingHTTPServer=server
ingress.emit=lambda state: print(json.dumps({'state':state,'port':created[0].server_port}),flush=True)
ingress.run('https','connector.example.org',Path(os.environ['HOME'])/'ingress')
'''
        parent_code = '''import json,os,subprocess,sys,signal
env=dict(os.environ,DBOND_INGRESS_PARENT_PID=str(os.getpid()))
child=subprocess.Popen([sys.executable,'-c',os.environ['QA_CHILD_CODE']],env=env,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
row=json.loads(child.stdout.readline());row['pid']=child.pid
print(json.dumps(row),flush=True)
signal.pause()
'''
        with tempfile.TemporaryDirectory() as root:
            env = dict(os.environ, HOME=root, QA_INGRESS_MODULE=str(Path(__file__).parent), QA_CHILD_CODE=child_code)
            parent = subprocess.Popen([sys.executable, "-c", parent_code], env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True)
            child_pid = None
            queue = select.kqueue()
            try:
                self.assertTrue(select.select([parent.stdout], [], [], 5)[0], "fixture helper did not start")
                row = json.loads(parent.stdout.readline())
                self.assertEqual(row["state"], "restricted_proxy_ready")
                child_pid, port = row["pid"], row["port"]
                self.assertNotIn(port, (ingress.PORT, ingress.UPSTREAM_PORT))
                with socket.create_connection(("127.0.0.1", port), timeout=1): pass
                queue.control([select.kevent(child_pid, filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT, fflags=select.KQ_NOTE_EXIT)], 0, 0)
                parent.kill(); parent.wait(timeout=2)
                self.assertTrue(queue.control([], 1, 5), "app crash left its ingress helper alive")
                with self.assertRaises(OSError): socket.create_connection(("127.0.0.1", port), timeout=1)
                child_pid = None
            finally:
                queue.close()
                if parent.poll() is None:
                    parent.kill(); parent.wait(timeout=2)
                if child_pid:
                    try: os.kill(child_pid, signal.SIGTERM)
                    except ProcessLookupError: pass
                parent.stdout.close(); parent.stderr.close()

    def test_tunnel_token_is_inherited_never_argv_or_status(self):
        token = "private-fixture-tunnel-token"
        signals = {}
        child = Mock()
        child.poll.return_value = 0
        child.stderr = io.BytesIO()
        child.wait.side_effect = lambda: signals[ingress.signal.SIGTERM]()
        output = io.StringIO()
        with patch.dict(os.environ, {"TUNNEL_TOKEN": token}), \
             patch.object(ingress, "ThreadingHTTPServer", return_value=Mock()), \
             patch.object(ingress, "ParentWatch", return_value=Mock()), \
             patch.object(ingress, "install_cloudflared", return_value=Path("/private/fixture/cloudflared")), \
             patch.object(ingress.signal, "signal", side_effect=lambda number, callback: signals.update({number: callback})), \
             patch.object(ingress.subprocess, "Popen", return_value=child) as launch, \
             contextlib.redirect_stdout(output):
            ingress.run("cloudflare", "connector.example.org", Path("/private/fixture"))
        self.assertNotIn(token, json.dumps(launch.call_args.args))
        self.assertEqual(launch.call_args.kwargs["env"]["TUNNEL_TOKEN"], token)
        self.assertNotIn(token, output.getvalue())
        self.assertIn("restricted_proxy_ready", output.getvalue())

    def test_invalid_tunnel_token_does_not_retry(self):
        child = Mock()
        child.poll.return_value = 1
        child.stderr = io.BytesIO(b"Provided tunnel token is not valid.\n")
        with patch.dict(os.environ, {"TUNNEL_TOKEN": "fixture-token"}), \
             patch.object(ingress, "ThreadingHTTPServer", return_value=Mock()), \
             patch.object(ingress, "ParentWatch", return_value=Mock()), \
             patch.object(ingress, "install_cloudflared", return_value=Path("/private/fixture/cloudflared")), \
             patch.object(ingress.signal, "signal"), \
             patch.object(ingress.subprocess, "Popen", return_value=child) as launch, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError):
                ingress.run("cloudflare", "connector.example.org", Path("/private/fixture"), backoff=(0, 0, 0))
        self.assertEqual(launch.call_count, 1)

    def test_tunnel_crashes_have_bounded_recovery_budget(self):
        def process(*_args, **_kwargs):
            child = Mock()
            child.poll.return_value = 1
            child.stderr = io.BytesIO()
            return child
        with patch.dict(os.environ, {"TUNNEL_TOKEN": "fixture-token"}), \
             patch.object(ingress, "ThreadingHTTPServer", return_value=Mock()), \
             patch.object(ingress, "ParentWatch", return_value=Mock()), \
             patch.object(ingress, "install_cloudflared", return_value=Path("/private/fixture/cloudflared")), \
             patch.object(ingress.signal, "signal"), \
             patch.object(ingress.subprocess, "Popen", side_effect=process) as launch, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                ingress.run("cloudflare", "connector.example.org", Path("/private/fixture"), backoff=(0, 0, 0))
        self.assertEqual(launch.call_count, 4)

    def test_parent_watch_refuses_nonparent_pid(self):
        with self.assertRaises(ValueError):
            ingress.ParentWatch(lambda: None, expected=1)

    def test_pinned_release_has_arch_specific_sha256(self):
        for name, digest in ingress.ARCHIVES.values():
            self.assertEqual(len(digest), 64)
            self.assertTrue(name.startswith("cloudflared-darwin-"))

    def test_checksum_mismatch_never_extracts_or_runs(self):
        response = io.BytesIO(b"untrusted archive")
        response.url = "https://release-assets.githubusercontent.com/fixture"
        opener = type("Opener", (), {"open": lambda *_args, **_kwargs: response})()
        with tempfile.TemporaryDirectory() as root, patch.object(ingress.platform, "system", return_value="Darwin"), \
             patch.object(ingress.platform, "machine", return_value="arm64"), \
             patch.object(ingress.urllib.request, "build_opener", return_value=opener), patch.object(ingress.tarfile, "open") as extract:
            with self.assertRaises(ValueError): ingress.install_cloudflared(Path(root))
            extract.assert_not_called()
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_verified_archive_extracts_only_regular_binary_and_cache_checks_hash(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
            member = tarfile.TarInfo("cloudflared")
            member.size = 7
            bundle.addfile(member, io.BytesIO(b"fixture"))
        data = archive.getvalue()
        response = io.BytesIO(data)
        response.url = "https://release-assets.githubusercontent.com/fixture"
        opener = type("Opener", (), {"open": lambda *_args, **_kwargs: response})()
        with tempfile.TemporaryDirectory() as root, patch.object(ingress.platform, "system", return_value="Darwin"), \
             patch.object(ingress.platform, "machine", return_value="arm64"), \
             patch.dict(ingress.ARCHIVES, {"arm64": ("fixture.tgz", hashlib.sha256(data).hexdigest())}), \
             patch.object(ingress.urllib.request, "build_opener", return_value=opener):
            installed = ingress.install_cloudflared(Path(root))
            self.assertEqual(installed.read_bytes(), b"fixture")
            self.assertEqual(installed.stat().st_mode & 0o777, 0o700)
            self.assertEqual(ingress.install_cloudflared(Path(root)), installed)
            installed.write_bytes(b"changed")
            with self.assertRaises(ValueError): ingress.install_cloudflared(Path(root))

    def test_hostname_rejects_urls_credentials_and_local_addresses(self):
        for bad in ("https://connector.example.org", "127.0.0.1", "localhost", "name.local", "user@host.example.org", "host.example.org/path"):
            with self.assertRaises(ValueError): ingress.hostname(bad)
        self.assertEqual(ingress.hostname("Connector.Example.Org"), "connector.example.org")


if __name__ == "__main__": unittest.main()
