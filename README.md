# D-Bond

Public source edition prepared for `ivg-design/dbond`.

D-Bond is a macOS menu bar broker for locally paired services. Its connection wizard
shows each service's loopback endpoint and a one-time bearer credential. Pairings,
requests, and receipts stay in the Mac user's local store. A stored request is not
proof that Dot received, acknowledged, or answered it.

## Install

Requires macOS 13+, Swift, and Python 3.9+. Build with `swift build`, then run
`bash scripts/install_app.sh` to create and install an ad hoc signed app in
`~/Applications`. Open D-Bond, then choose **Add Connection…**. Select the
local demo destination to try the broker without a Dot connection. The wizard
lets you copy the endpoint and one-time credential for the paired service.
See the [connection guide](docs/CONNECTIONS.md) for service setup, owner SOP
rules, and event trace interpretation.

For Dot delivery, you must separately install and authenticate a compatible
Dot Bridge Transport host with the `d-bond` native channel, its loopback API,
and its local SQLite store. Set `DBOND_DOT_PYTHON` to that host's Python
interpreter if it differs from the conventional
`~/Library/Application Support/DotBridge/venv/bin/python` path. Set
`DBOND_DOTBRIDGE_DIR` if its `bridge.toml` and `bridge.sqlite3` are elsewhere.
This repository does not provide that host, its account, the Dot app binding,
subscription, or a standalone Dot transport. Dot availability and scheduling
depend on the installed host and account permissions.
An operator who already owns a connected Dot app can use the
[private Dot plugin packager](adapters/dot/README.md) with their own app binding.
Its ZIP stays outside this repository and does not create a Dot connection.

The [CLI](adapters/cli/README.md) can enroll through the app's private owner
socket or link a wizard pairing. For example:

```sh
python3 adapters/cli/dbond.py enroll review --service Codex --topic 'Review chat'
python3 adapters/cli/dbond.py ask review 'What should I check?'
python3 adapters/cli/dbond.py listen review
```

The [Claude adapter](adapters/claude/README.md) supports a local Desktop MCP
is an isolated Chrome client, not an installed
reads and edits owner SOP rules and event traces through the app's private
same-user socket. A [Vollna example](examples/vollna/README.md) forwards one
versioned job from an existing authenticated receiver; it is not a hosted
webhook or an active cutover. Generic clients POST `{"id":"stable-id","payload":{...}}`
to the wizard endpoint with `Authorization: Bearer <credential>`, then read
`<endpoint>/stream?after=0` with the same credential for receipts. Reuse an
unchanged ID and payload after an uncertain POST.

To add the local control plugin from the planned public repository, run
`codex plugin marketplace add ivg-design/dbond`, then
`codex plugin add d-bond-local-control@dbond-github`. This plugin controls only
your local D-Bond owner socket and does not install a Dot connection.

Run `swift build`, `python3 -m unittest discover -s scripts -p 'test_*.py'`,
`python3 -m unittest discover -s adapters/cli -p 'test_*.py'`, and
`python3 scripts/test_owner_sop.py` for the policy and trace regression.
`scripts/smoke.py` exercises
the compiled probe with disposable stores. These checks do not establish live
Dot delivery or installed app acceptance.

No license is supplied with this edition.

The [marketplace evaluation](docs/MARKETPLACE.md) describes a possible future
public listing and its open requirements; it is not a submitted plugin.
