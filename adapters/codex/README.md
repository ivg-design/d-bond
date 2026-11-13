# D-Bond Local Control for Codex

This local Codex plugin exposes six natural-language MCP tools: check local app status, list connections,
read a connection SOP, replace it with an expected-version check, list recent
event traces, and inspect one trace. The tools run in Codex and connect only to
the same-user D-Bond Unix owner socket. They do not use a service pairing secret
or offer a remote administration endpoint. The separate **D-Bond for Dot**
account plugin handles Dot messaging; this plugin handles owner configuration
and trace on this Mac.

## Install and update

Build the self-contained plugin from the project root:

```sh
python3 adapters/codex/build.py
codex plugin marketplace add /absolute/path/to/D-Bond/adapters/codex
codex plugin add d-bond-local-control@d-bond-local
```

The marketplace source is the local `adapters/codex` directory. The install is
reversible with `codex plugin remove d-bond-local-control@d-bond-local` and
`codex plugin marketplace remove d-bond-local`. For updates, rebuild the source,
then use the Codex plugin update/reinstall flow; do not hand-edit its cache.
Codex may need a new chat to discover newly installed tools.

The plugin's six MCP tools are the implemented **inside Codex** controls. The
Codex plugin listing is host-provided UI. There is no custom settings pane or
MCP App in this package; no evidence yet shows that a host settings extension
can safely own this local socket without adding another runtime.

## Use

Call `local_status` without a connection ID to check the existing Mac install.
It returns only `installation` (`installed`, `uninstalled`, or `unknown`) and
`service` (`reachable`, `offline`, or `unknown`). `offline` means the app was
found but its owner API did not answer; an older app without that API can also
produce this state. `unknown` means discovery could not settle the answer.
The check is explicit, bounded, and read-only; MCP startup does not probe the
app. It does not enroll a chat or expose paths, connections, or credentials.

Ask Codex to list D-Bond connections, then name one by its exact connection ID.
Read its SOP before changing it. A missing SOP has version 0. For a new policy,
submit version 1 and `expectedVersion: 0`; for each later edit, increase the
version by one and supply the version you read. An empty rules array can express
a versioned policy with no active rules. If a version conflict occurs, read the
current SOP again and reconcile the rules before retrying.

Trace tools report observed event states and the SOP **version** captured for
the event, not the full policy text. Times use this Mac’s timezone and omit
fractions of a second; the owner-socket protocol retains precise UTC values.
A receipt state is not proof of downstream
acknowledgement or delivery. Event text and SOP text are external content, never
instructions to the Codex assistant.

The D-Bond app must be running with the current owner-socket API. A missing
socket means it is not running or the installed build lacks the API. An owner
reply of `Pairing refused` means an older app build still handles only pairing.
The client checks owner-only directory/socket permissions before connecting.

## Verify

```sh
python3 -m unittest adapters.codex.test_mcp -v
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26"}}' '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | /bin/sh adapters/codex/plugins/d-bond-local-control/server/launch.sh
codex plugin list --json
```

The first two checks test local protocol behavior. The last checks installation;
use the tools in a fresh Codex chat to confirm host discovery and live D-Bond
operation.
