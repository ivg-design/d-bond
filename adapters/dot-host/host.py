#!/usr/bin/env python3
"""Private D-Bond Dot host management. Status never exposes configuration secrets."""
from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import json
import os
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE / "vendor")]
DEFAULT_ROOT = Path.home() / "Library/Application Support/D-Bond/Dot Connector"
PORT = 18769


def private_dir(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Connector state directory must be owner-only")


def read_config(root):
    fd = os.open(root / "bridge.toml", os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 16384:
            raise ValueError("Connector configuration must be a small owner-only file")
        value = tomllib.load(stream)
    if value.get("host") != "127.0.0.1" or value.get("port") != PORT:
        raise ValueError("Connector must use its fixed private loopback bind")
    if Path(value.get("db_path", "")) != root / "bridge.sqlite3":
        raise ValueError("Connector store must remain in its own state directory")
    return value


def save_config(root, values):
    private_dir(root)
    fd, temporary = tempfile.mkstemp(prefix=".config-", dir=root)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            for name, value in values.items():
                stream.write(f"{name} = {json.dumps(value)}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / "bridge.toml")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_urls(public_url, redirect_uri):
    origin = urlsplit(public_url.rstrip("/"))
    redirect = urlsplit(redirect_uri)
    if (origin.scheme != "https" or not origin.hostname or origin.path or origin.query or origin.fragment
            or origin.username or origin.password or any(c.isspace() for c in public_url)
            or origin.port not in (None, 443)):
        raise ValueError("Provide a public HTTPS origin without a path")
    if (redirect.scheme != "https" or not redirect.hostname or redirect.username or redirect.password
            or redirect.fragment or any(c.isspace() for c in redirect_uri)):
        raise ValueError("Provide the exact HTTPS OAuth redirect URI from plugin registration")
    return public_url.rstrip("/"), redirect_uri


@contextmanager
def configuration_lock(root):
    private_dir(root)
    fd = os.open(root / "configure.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def new_config(root):
    return {"owner_key": secrets.token_urlsafe(32), "api_key": secrets.token_urlsafe(32),
            "storage_key": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
            "owner_user_id": "dbond-" + secrets.token_hex(16), "client_id": "d-bond",
            "host": "127.0.0.1", "port": PORT, "db_path": str(root / "bridge.sqlite3"),
            "reply_timeout_seconds": 604800}


def configure(root, public_url, redirect_uri):
    public_url, redirect_uri = validate_urls(public_url, redirect_uri)
    with configuration_lock(root):
        try:
            values = read_config(root)
            if connector_status(root).get("running") and (values["public_base_url"], values.get("redirect_uri")) != (public_url, redirect_uri):
                raise ValueError("Stop the built-in connector before changing its public identity")
        except FileNotFoundError:
            values = new_config(root)
        values.update(public_base_url=public_url, redirect_uri=redirect_uri)
        from dots_bridge.config import Settings
        Settings.model_validate(values)
        save_config(root, values)
    return connector_status(root)


def configure_tunnel(root, tunnel_id):
    from tunnel import provision
    from dots_bridge.config import Settings
    with configuration_lock(root):
        try:
            values = read_config(root)
        except FileNotFoundError:
            values = new_config(root)
            values["public_base_url"] = f"http://127.0.0.1:{PORT}"
        Settings.model_validate(values)
        provision(root, tunnel_id)
        if not (root / "bridge.toml").exists():
            save_config(root, values)
    return connector_status(root)


def connector_status(root):
    import tunnel
    result = {"configured": False, "running": False, "subscribed": False, "registered": False,
              "public_url": "", "redirect_uri": "", "mcp_url": "", "route": "owned",
              "tunnel_configured": False, "tunnel_mcp_url": "", "tunnel_id": "",
              "identity": {"connected": False, "active_subscriptions": 0, "tunnel_id": "", "tunnel_listening": False, "tunnel_subscribed": False}, "state": "not_configured", "reason": None}
    try:
        values = read_config(root)
    except FileNotFoundError:
        return result
    public = values["public_base_url"] if values.get("redirect_uri") else ""
    binding = tunnel.current_binding(root)
    key = tunnel.credential_for(root)
    tunnel_configured = bool(binding and key and hashlib.sha256(key.encode()).hexdigest() == binding.key_hash)
    result.update(configured=True, public_url=public, redirect_uri=values.get("redirect_uri", ""),
                  mcp_url=(public + "/mcp") if public else "", state="stopped", tunnel_configured=tunnel_configured,
                  tunnel_id=binding.tunnel_id if binding else "",
                  tunnel_mcp_url=f"http://127.0.0.1:{tunnel.PORT}/mcp" if tunnel_configured else "")
    # Authenticated identity check: an unrelated process on this port never counts as this connector.
    request = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/host-status",
                                    headers={"Authorization": "Bearer " + values["api_key"]})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=2) as response:
            data = json.loads(response.read(4097))
        if data.get("host") != "d-bond-dot-host/1" or data.get("store_id") != values["owner_user_id"]:
            raise ValueError("Foreign host identity")
        count = data.get("active_subscriptions")
        if type(count) is not int or count < 0:
            raise ValueError("Invalid subscription status")
        subscribed = count == 1 and data.get("subscribed") is True
        pid = data.get("pid")
        if type(pid) is int and 1 < pid <= 2**31 - 1:
            result["pid"] = pid
        result.update(running=True, subscribed=subscribed, registered=subscribed,
                      identity={"connected": subscribed, "active_subscriptions": count,
                                "tunnel_id": data.get("tunnel_id", ""),
                                "tunnel_listening": tunnel_configured and data.get("tunnel_id") == binding.tunnel_id and data.get("tunnel_listening") is True,
                                "tunnel_subscribed": subscribed and tunnel_configured and data.get("tunnel_id") == binding.tunnel_id and data.get("tunnel_subscribed") is True},
                      state="subscribed" if subscribed else "waiting_subscription")
    except (OSError, ValueError, urllib.error.URLError):
        result["reason"] = "connector_unavailable"
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def verify_vendor():
    manifest = json.loads((HERE / "upstream.json").read_text())
    for name, expected in manifest["files"].items():
        if hashlib.sha256((HERE / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Pinned upstream source checksum mismatch")


def serve(root):
    private_dir(root)
    lock = os.open(root / "host.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        verify_vendor()
        from dots_bridge.config import Settings
        from core import create_app
        import uvicorn
        settings = Settings.model_validate(read_config(root))
        # Bind before initializing stores; a foreign service cannot trigger duplicate host startup.
        import tunnel
        credential = tunnel.credential_for(root) if tunnel.current_binding(root) else None
        with ExitStack() as stack:
            ports = (PORT, tunnel.PORT) if credential else (PORT,)
            sockets = []
            for port in ports:
                bound = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
                bound.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                bound.bind(("127.0.0.1", port))
                bound.listen(128)
                sockets.append(bound)
            owner_app = create_app(settings)
            asgi_app = owner_app
            if credential:
                private_mcp = tunnel.create_app(settings, owner_app.state.bridge, credential, tunnel.PORT)
                asgi_app = tunnel.attach(owner_app, private_mcp, tunnel.PORT)
            server = uvicorn.Server(uvicorn.Config(asgi_app, host="127.0.0.1", port=PORT,
                workers=1, access_log=False, log_level="warning", proxy_headers=False))
            owner_app.state.shutdown_callback = lambda: setattr(server, "should_exit", True)
            import asyncio
            asyncio.run(server.serve(sockets=sockets))
            if not server.started:
                raise RuntimeError("Connector startup failed")
    finally:
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("configure")
    setup.add_argument("--public-url", required=True)
    setup.add_argument("--redirect-uri", required=True)
    sub.add_parser("configure-tunnel").add_argument("--tunnel-id", required=True)
    for command in ("status", "serve", "stop", "setup", "provision"):
        sub.add_parser(command)
    args = parser.parse_args()
    os.umask(0o077)
    root = args.state_dir.expanduser().absolute()
    try:
        if args.command == "configure":
            result = configure(root, args.public_url, args.redirect_uri)
        elif args.command == "configure-tunnel":
            result = configure_tunnel(root, args.tunnel_id)
        elif args.command == "serve":
            serve(root)
            return
        elif args.command == "setup":
            values = read_config(root)
            if not values.get("redirect_uri"):
                raise ValueError("This host uses private OpenAI tunnel setup")
            result = {"mcp_url": values["public_base_url"] + "/mcp", "client_id": values["client_id"],
                      "client_secret": "", "owner_key": values["owner_key"],
                      "authorization_url": values["public_base_url"] + "/authorize"}
        elif args.command == "stop":
            result = connector_status(root)
            if result["running"]:
                values = read_config(root)
                request = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/host-stop", method="POST", data=b"{}",
                    headers={"Authorization": "Bearer " + values["api_key"], "Content-Type": "application/json"})
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
                with opener.open(request, timeout=2) as response:
                    stopped = json.loads(response.read(4097))
                if stopped.get("store_id") != values["owner_user_id"] or stopped.get("stopping") is not True:
                    raise ValueError("Host did not confirm shutdown")
                result.update(state="stopping", subscribed=False)
        elif args.command == "provision":
            if not (HERE.parent / "dot-runtime/python/bin/python3.13").is_file():
                subprocess.run([str(HERE / "provision"), str(root)], check=True, stdout=subprocess.DEVNULL)
            result = connector_status(root)
        else:
            result = connector_status(root)
        print(json.dumps(result, separators=(",", ":")), flush=True)
    except Exception as exc:
        # Never include exception strings: config validation and HTTP errors can contain private material.
        print(json.dumps({"state": "blocked", "reason": type(exc).__name__, "configured": False,
                          "running": False, "subscribed": False, "route": "owned"}), flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
