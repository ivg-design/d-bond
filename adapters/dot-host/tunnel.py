"""Private OpenAI tunnel ingress. Only the owned MCP path is reachable on this listener."""
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
import hmac
import hashlib
import json
import re
import os
import secrets
import stat
import tempfile

from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Mount

from core import TUNNEL_REQUEST, add_acknowledgement_tool
from dots_bridge.app import RequestGuard
from dots_bridge.mcp import PROTOCOL_VERSION, _EventsExtension, create_mcp
from fastmcp.server.extensions import build_method_handler
from pydantic import BaseModel

PORT = 18773
HEADER = "x-dbond-tunnel"
KEY_FILENAME = "tunnel-ingress.key"
# The registered cloud callback used by ChatGPT MCP Events. webhooks.post_webhook
# additionally verifies TLS, pins public DNS, and forbids redirects.
CALLBACK_HOST = "connectors.api.openai.com"
EVENT_VERSIONS = frozenset({"2025-03-26", "2025-06-18", "2025-11-25", PROTOCOL_VERSION})


BINDING_FILENAME = "tunnel-binding.json"
ID_HEADER = "x-dbond-tunnel-id"
ID_PATTERN = re.compile(r"tunnel_(?:[a-z0-9]{4}_)?[a-z0-9]{32}")


@dataclass(frozen=True)
class Binding:
    tunnel_id: str
    generation: str
    key_hash: str


def read_private(root, name):
    try:
        fd = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 2048):
            raise ValueError("Unsafe private tunnel file")
        return stream.read(2049)


def private_replace(root, name, content):
    fd, temporary = tempfile.mkstemp(prefix=".tunnel-", dir=root)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / name)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def credential_for(root):
    data = read_private(root, KEY_FILENAME)
    if data is None:
        return None
    if not re.fullmatch(rb"[a-f0-9]{64}", data):
        raise ValueError("Invalid tunnel ingress key")
    return data.decode("ascii")


def current_binding(root):
    data = read_private(root, BINDING_FILENAME)
    if data is None:
        return None
    binding = Binding(**json.loads(data))
    if (not ID_PATTERN.fullmatch(binding.tunnel_id) or not re.fullmatch(r"[a-f0-9]{32}", binding.generation)
            or not re.fullmatch(r"[a-f0-9]{64}", binding.key_hash)):
        raise ValueError("Invalid tunnel selection")
    return binding


def provision(root, tunnel_id):
    # Caller holds the host configuration lock. Publish a new generation before
    # its key so stale listeners immediately refuse requests. If interrupted,
    # repeating configure repairs the incomplete key without trusting old grants.
    if not isinstance(tunnel_id, str) or not ID_PATTERN.fullmatch(tunnel_id):
        raise ValueError("Invalid OpenAI tunnel ID")
    old = current_binding(root)
    key = credential_for(root)
    if old and old.tunnel_id == tunnel_id and key and old.key_hash == hashlib.sha256(key.encode()).hexdigest():
        return old
    new_key = secrets.token_hex(32).encode()
    selected = Binding(tunnel_id, secrets.token_hex(16), hashlib.sha256(new_key).hexdigest())
    private_replace(root, BINDING_FILENAME, json.dumps(vars(selected)).encode())
    private_replace(root, KEY_FILENAME, new_key)
    return selected


async def tunnel_capabilities(ctx, call_next):
    result = await call_next(ctx)
    if ctx.method not in ("initialize", "tools/list"):
        return result
    if isinstance(result, BaseModel):
        result = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    if not isinstance(result, dict):
        return result
    if ctx.method == "initialize":
        return {**result, "capabilities": {**result.get("capabilities", {}), "events": {}}}
    # OAuth is kept on the public ingress. For a dedicated private tunnel the
    # client authenticates on every request at the outer boundary below.
    schemes = [{"type": "noauth"}]
    return {**result, "tools": [{**tool, "securitySchemes": schemes,
        "_meta": {**tool.get("_meta", {}), "securitySchemes": schemes}} for tool in result.get("tools", [])]}


def create_app(settings, bridge, credential, port=None):
    port = PORT if port is None else port
    if not isinstance(credential, str) or len(credential) != 64:
        raise ValueError("Missing tunnel ingress key")
    expected = credential.encode("ascii")
    root = settings.db_path.parent
    binding = current_binding(root)
    if binding is None or binding.key_hash != hashlib.sha256(expected).hexdigest():
        raise ValueError("Unbound tunnel ingress key")
    mcp = create_mcp(bridge, auth=None)
    add_acknowledgement_tool(mcp, bridge, oauth=False)
    # The vendored host's Events methods target MCP 2.0 by default. ChatGPT
    # also supports Events over the stateless legacy initialize handshake.
    for method in _EventsExtension(bridge, authenticated=False).methods():
        compatible = replace(method, protocol_versions=EVENT_VERSIONS)
        mcp._mcp_server.add_request_handler(method.method, method.params_type, build_method_handler(compatible))
    mcp._mcp_server.middleware.insert(0, tunnel_capabilities)
    protocol = mcp.http_app(path="/mcp", json_response=True, stateless_http=True,
                            allowed_hosts=["127.0.0.1"], allowed_origins=[])
    payload_limited = RequestGuard(protocol, allowed_origins=set())
    loopback = f"127.0.0.1:{port}".encode()

    async def gate(scope, receive, send):
        if scope["type"] == "lifespan":
            return await protocol(scope, receive, send)
        if scope["type"] != "http" or scope["path"] != "/mcp" or scope["query_string"]:
            # The official client uses the 404 body to finish optional OAuth discovery.
            return await Response("Not Found", status_code=404, media_type="text/plain")(scope, receive, send)
        if scope["method"] != "POST":
            return await Response(status_code=405, headers={"Allow": "POST"})(scope, receive, send)
        headers = scope["headers"]
        if ([v for k, v in headers if k == b"host"] != [loopback]
                or any(k == b"origin" for k, _ in headers)):
            return await Response(status_code=403)(scope, receive, send)
        keys = [v for k, v in headers if k == HEADER.encode()]
        ids = [v for k, v in headers if k == ID_HEADER.encode()]
        if (len(keys) != 1 or not hmac.compare_digest(keys[0], expected)
                or ids != [binding.tunnel_id.encode()] or current_binding(root) != binding):
            return await Response(status_code=401)(scope, receive, send)
        token = TUNNEL_REQUEST.set(binding)
        try:
            return await payload_limited(scope, receive, send)
        finally:
            TUNNEL_REQUEST.reset(token)

    gate.binding = binding
    gate.protocol_lifespan = lambda: protocol.router.lifespan_context(protocol)
    return gate


def attach(owner_app, ingress_app, ingress_port):
    """Share one Store and worker, while isolating the two bound sockets by port."""
    @asynccontextmanager
    async def lifespan(_):
        async with owner_app.router.lifespan_context(owner_app):
            # Only this context owns the durable delivery worker.
            async with ingress_app.protocol_lifespan():
                owner_app.state.tunnel_binding = ingress_app.binding
                owner_app.state.tunnel_listening = True
                try:
                    yield
                finally:
                    owner_app.state.tunnel_listening = False

    async def dispatch(scope, receive, send):
        app = ingress_app if scope["server"][1] == ingress_port else owner_app
        await app(scope, receive, send)

    return Starlette(routes=[Mount("/", app=dispatch)], lifespan=lifespan)
