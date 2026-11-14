# D-Bond

A macOS menu bar bridge for Dot, local services, and individual agent chats.
Pair services once, send addressed messages, and inspect delivery receipts in one
place. Direct messages between paired services work independently of Dot.

**Version 0.3.10** · [Changelog](CHANGELOG.md) · [All releases](https://github.com/ivg-design/d-bond/releases)

## Install

Download **D-Bond-macOS-arm64.zip** from [the latest GitHub Release](https://github.com/ivg-design/d-bond/releases/latest),
extract it, and move **D-Bond.app** to `~/Applications` before opening it. This
ZIP targets Apple silicon; build from source for another Mac architecture. The
release also provides `d-bond-claude.mcpb` and `SHA256SUMS.txt`. The app runs in
the menu bar; choose **Open Connections…** to get started.

Requirements: **macOS 13 or later** and **Python 3.11 or later** for the bundled
adapters and Dot bridge helper. Install the corresponding Codex or Claude host
only for the integrations you use. Check the release's architecture before
installing it; source builds target your Mac. Dragging the app into Applications
does not install the CLI shortcut or a crash supervisor. **Start at Login** can
be enabled in the app settings; the source installer below adds supervision
and the CLI shortcut.

Current builds are **ad hoc signed and not notarized**. macOS may require approval
in **System Settings → Privacy & Security** after you first try to open a downloaded
copy. Approve only a download you trust.

To build and install from source, install Xcode Command Line Tools with Swift
5.9 or later, ensure `python3` is available, then run:

```sh
git clone https://github.com/ivg-design/d-bond.git
cd d-bond
bash scripts/install_app.sh
```

Quit a running D-Bond before upgrading. The source installer builds the app,
installs it into `~/Applications`, links `~/.local/bin/dbond`, and configures a
per-user launchd job. Existing pairings and receipts are retained. Remove any
older duplicate **Open at Login** entry; use D-Bond's Settings to control startup.
To build the app without installing it, run `bash scripts/bundle.sh`; the result
is `dist/D-Bond.app`. See the [connection guide](docs/CONNECTIONS.md) for details.

## Connect a service or chat

1. Open **Connections → Add Connection…** and try **Local demo echo**, or configure
   a service with its loopback endpoint and one-time credential.
2. Use **Connect agents** to install the appropriate packaged connector. The host
   still controls its approval and discovery; Codex may require a new chat.
3. Inside each intended Codex Desktop or Claude Code chat, say **“Register this
   session in D-Bond.”** Registration addresses that exact chat, rather than every
   chat in the app. Claude Code may require channel opt-in or a Monitor.
4. Ask a paired chat to message another connection by its displayed name. Use
   **Events** to inspect storage, host acceptance, recipient acknowledgment, and
   replies separately.

You can also install Codex Local Control from this repository:

```sh
codex plugin marketplace add ivg-design/d-bond
codex plugin add d-bond-local-control@dbond-github
```

The [CLI guide](adapters/cli/README.md) covers manual pairing and diagnostics.
[Codex Local Control](adapters/codex/README.md) provides local connection/rule
management and messaging. [Claude](adapters/claude/README.md) and
[generic direct clients](adapters/direct/README.md) have their own setup guides.
[Vollna sender](examples/vollna/README.md) is an example for an existing receiver;
creating a pairing does not install those products or change their workflows.

## Dot and delivery limits

To wake **your existing Dot**, separately install and authenticate a compatible
**Dot Bridge Transport** host with the native `d-bond` channel, loopback API,
SQLite store, and active subscription. This repository does not supply that
transport, an account, a Dot app binding, or another person's connected services.
Its [private plugin packager](adapters/dot/README.md) is for operators who already
own a working app binding.

The conventional host interpreter is
`~/Library/Application Support/DotBridge/venv/bin/python`. Set `DBOND_DOT_PYTHON`
for another interpreter and `DBOND_DOTBRIDGE_DIR` for another directory containing
`bridge.toml` and `bridge.sqlite3`.

The [Codex listener](adapters/codex-session/README.md) depends on a private,
version-sensitive Desktop IPC interface. A durable/cloud chat currently needs
an **open, loaded Desktop owner**. When that owner is unavailable, the listener
retains the observed event and reports its pending sequence, reason, and local
owner-availability timestamps. It retries while the message is pending; opening
the chat may be necessary before delivery can proceed. An idle listener is not
proof that a closed cloud chat can wake. A Dot-owned task route is **under
evaluation**, without an accepted closed-chat wake guarantee.

**Stored by D-Bond** means persisted for replay. Host acceptance, agent access,
a content reply, and completion of requested work are different evidence.
Account grants, host availability, and computer power state can prevent delivery.

## Develop

```sh
swift build
swift test
swift build -c release --product dbond-probe
python3 -B -m unittest discover -s Tests
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 -m unittest discover -s adapters/cli -p 'test_*.py'
python3 -m unittest discover -s adapters/codex-session -p 'test_*.py'
```

To reproduce the release files for your Mac's architecture from a tagged revision,
run `bash scripts/package_release.sh v0.3.10`. Find the app archive, Claude extension,
and checksums in `dist/releases/v0.3.10/`.

The [marketplace evaluation](docs/MARKETPLACE.md) records a possible future listing;
this repository is not a submitted public Dot service.

## License

D-Bond is available under the [MIT License](LICENSE), copyright 2026 IVG Design.
See [third-party notices](THIRD_PARTY_NOTICES.md) for upstream attribution and
service artwork rights.
