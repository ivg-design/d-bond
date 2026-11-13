# D-Bond

Public source edition prepared for `ivg-design/dbond`.

D-Bond is a macOS menu bar broker for locally paired services. Its connection wizard
shows each service's loopback endpoint and a one-time bearer credential. Pairings,
requests, and receipts stay in the Mac user's local store. A stored request is not
proof that Dot received, acknowledged, or answered it.

## Install

Requires macOS 13+, Swift, and Python 3.9+. Build with `swift build`, then run
`bash scripts/install_app.sh` to create and install an ad hoc signed app in
`~/Applications`. Open D-Bond → **Open Connections** → **Add Connection…**. Select the
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

With the connector enabled, say **“Register this session in D-Bond”** inside
each Codex Desktop or Claude Code chat you want to reach. The connector pairs
the current chat with the Mac and attaches that chat's listener. Claude may
ask you to allow its Monitor. Open **Connections** and look for a verified
wake check on each chat. You can then ask either chat to send a question
directly to the other by its displayed name. Each chat keeps its own address
when several sessions use the same service.

Open **Events** on either connection for the ten latest entries. Each message
shows its direction, the other connection, local time to the second, and up
to two sentences (210 characters). Fenced code begins with a one-line preview.
**View complete log** includes older entries, message IDs and reply references.
The [CLI guide](adapters/cli/README.md) covers manual pairing and diagnostics.

The [Claude adapter](adapters/claude/README.md) supports a local Desktop MCP
extension and a Claude Code plugin with a session-start hook. A Claude Code
channel requires preview opt-in in an open session. The
[Codex Desktop listener](adapters/codex-session/README.md) is a separate,
opt-in per-chat adapter. It delivers the sender, original text and reply
reference through the discovered Desktop owner, steering an active turn or
requesting a turn for an idle, open chat. Host acceptance, recipient readback
is an isolated Chrome client, not an installed
reads and edits owner SOP rules and event traces through the app's private
same-user socket; its direct tools can send and read authenticated messages.
A [Vollna example](examples/vollna/README.md) forwards one
versioned job from an existing authenticated receiver; it is not a hosted
webhook or an active cutover. Generic clients POST `{"id":"stable-id","payload":{...}}`
to the wizard endpoint with `Authorization: Bearer <credential>`, then read
`<endpoint>/stream?after=0` with the same credential for receipts. Reuse an
unchanged ID and payload after an uncertain POST.

To add the local control plugin from the public repository, run
`codex plugin marketplace add ivg-design/dbond`, then
`codex plugin add d-bond-local-control@dbond-github`. This plugin controls only
your local D-Bond owner socket and does not install a Dot connection.

Run `swift build`, `python3 -m unittest discover -s scripts -p 'test_*.py'`,
`python3 -m unittest discover -s adapters/cli -p 'test_*.py'`, and
`python3 scripts/test_owner_sop.py` for the policy and trace regression, and
`python3 Tests/test_event_log_pages.py` plus
`python3 Tests/test_wake_roundtrip.py` for isolated persisted event and wake
state checks. The Codex listener fixture is
`python3 adapters/codex-session/test_listener.py`; the Claude Code hook fixture
is `python3 -m unittest discover -s adapters/claude/session-hooks -p 'test_*.py'`.
`scripts/smoke.py` exercises
the compiled probe with disposable stores. These checks do not establish live
Dot delivery or installed app acceptance.

No license is supplied with this edition.

The [marketplace evaluation](docs/MARKETPLACE.md) describes a possible future
public listing and its open requirements; it is not a submitted plugin.
