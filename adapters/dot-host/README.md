# Internal Dot connector

This is D-Bond's own fixed `d-bond` MCP Events host. It requires no separately
installed DotBridge package, Python environment, Slack account or Beeper account.
Released apps use their embedded `Resources/dot-runtime/python` interpreter and
hash-locked dependencies. Source development can provision a private runtime with
the included `provision` script. Runtime downloads use pinned CPython archive
checksums; Python dependencies use `requirements.lock` with `--require-hashes`
and binary wheels only. An initial source setup needs network access.

The unchanged upstream core is pinned by revision and per-file SHA-256 in
`upstream.json`. D-Bond additions live in `core.py` and `host.py`; they supply the
explicit ACK and durable single-route behavior. See [upstream notices](UPSTREAM_NOTICE.md).

## App interface

The app invokes `connector [--state-dir ABSOLUTE_DIRECTORY] COMMAND`. Default
state is `~/Library/Application Support/D-Bond/Dot Connector`. Configuration and
the SQLite database remain there across app upgrades. Nothing reads, rewrites,
migrates or stops the owner's existing DotBridge installation.

- `configure --public-url https://connector.example --redirect-uri EXACT_HTTPS_URI`
  performs first setup or updates endpoint configuration. It preserves the owner,
  application and storage keys on later calls. Stop the host before changing a
  running endpoint identity. Configuration alone does not change D-Bond's selected
  transport or prove a Dot subscription.
- `status` performs a bounded, authenticated loopback identity check and returns
  safe JSON. It does not provision, install, subscribe or send. `configured`,
  `running`, `subscribed`, `registered`, `public_url`, `redirect_uri`, `mcp_url`,
  `route`, `state`, `reason` and `identity` describe separate facts. `subscribed`
  requires exactly one unexpired, verified callback on this host's single route.
- `setup` explicitly reveals the local owner approval key for the app's setup
  dialog. It returns `mcp_url`, `client_id`, `client_secret` (empty: this is a public
  PKCE client), `owner_key` and `authorization_url`. Do not log this output.
  `/authorize` requires the plugin's OAuth request parameters; the bare URL does
  not authorize an account. The owner key belongs in the hosted consent page,
  never a client-secret field.
- `serve` runs the foreground host on `127.0.0.1:18769`. The app supervises this
  child. A same-state nonblocking file lock and exclusive socket bind prevent
  duplicate hosts. No daemon, launch agent or global Python package is installed.
- `stop` verifies the configured host's authenticated identity and asks it to
  shut down. It cannot kill another process merely because it owns the port.
- `provision` is a source-development fallback. A released app uses its bundled
  runtime without creating another environment or invoking pip.

Only public MCP/OAuth routes may be forwarded by D-Bond's ingress layer. Keep
all `/api/*` owner endpoints private. A reachable HTTPS MCP origin, registered
plugin/app identity, OAuth consent and a Dot-created event subscription are
separate requirements. A local host cannot manufacture those cloud grants.

## Compatibility and storage

The local application API preserves `/api/messages`, `/api/subscriptions`, received
message cursors and upstream MCP tools. Subscriptions also return empty
`slack_channels` and `native_channels` lists: neither fallback exists here.
`acknowledge_request` stores a stable `messages.started_at`; callback `2xx` does
not set it. Replies and addressed pushes retain upstream `received_messages`
identities, sequence numbers, kinds and address metadata. A callback outage or
`410` preserves the request; explicit account revocation cancels outstanding
work. Stable request/event/idempotency IDs survive a restart and callback rebind.
Authenticated cancellation cannot supersede a different paired conversation.

OAuth scopes and hashed grants remain upstream-owned; callback destinations are
encrypted at rest. The owner config holds distinct owner approval, local API and
storage keys under owner-only permissions. Configuration, storage keys, callback
URLs/secrets and OAuth grants must never be placed in a release archive.

Run the isolated checks using the bundled interpreter:

```sh
/path/to/Resources/dot-runtime/python/bin/python3.13 -I -B adapters/dot-host/test_host.py
```

The checks use disposable stores, an in-process MCP/HTTP server and signed mock
callbacks. They cover ACK/reply/push, restart, callback replacement, authentication
and source integrity. They do not establish live cloud reachability or Dot wake.
