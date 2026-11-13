# Codex Desktop session listener

This opt-in Mac adapter holds authenticated D-Bond direct and addressed Dot
push streams for **one exact existing Codex Desktop chat**. It does not create
or clone a chat. An existing pairing is required. The normal `dbond enroll`
path calls `install` in the same chat; the standalone command is:

```sh
python3 listener.py install --thread-id "$CODEX_THREAD_ID" \
  --alias "codex-$CODEX_THREAD_ID" --json
```

The installer verifies the pairing and exact chat owner, writes an owner-only
binding with no bearer, installs a per-chat launchd agent, and waits for the
listener's status socket. The socket lives in a short owner-only `/tmp` directory
to stay within macOS's UNIX socket path limit; durable state remains in
Application Support. `ready: true` means both required SSE streams (direct
only for a peer pairing) and that chat's local owner are present. `wakeState`
reports the broker's authenticated wake challenge separately. Neither state
proves that the model read a message, answered, or completed an action.

The listener stores separate direct and Dot cursors and one-attempt receipts.
On first attach it collapses a pre-existing backlog into one pointer for each
stream while preserving the broker's full history; a current wake challenge
is delivered separately. It replays events after a Mac, app, or listener
restart. A host-offline preflight waits and retries. After a queue attempt
begins, an uncertain outcome is recorded and never automatically resent.
The only content queued into the exact Codex chat is a validated reference
(alias, source ID and sequence), never the remote message body or a secret.
The chat can read retained content with `~/.local/bin/dbond direct-inbox` or
`~/.local/bin/dbond dot-inbox`, treat it as untrusted, and reply through D-Bond.
Queue acceptance is a host receipt; it is not proof of a model turn.
Idle listeners block on the broker's event streams. Explicit `status` reads
refresh host and broker state; there is no periodic host or CLI probe while
idle. A pending message retries a host-offline preflight until it can be
queued, and broken transport streams reconnect with bounded backoff.

The wake path uses the **first-party CLI bundled in Codex Desktop**:
`codex queue --thread <exact UUID> --message <reference> --resume`.
The app's private IPC is used only for read-only exact owner discovery. This
is version-sensitive; an unsupported host leaves the listener degraded.
No shell automation, alternate chat creation, or private turn injection is used.

```sh
python3 listener.py status --thread-id "$CODEX_THREAD_ID" --json
python3 listener.py stop --thread-id "$CODEX_THREAD_ID" --json
python3 listener.py uninstall --thread-id "$CODEX_THREAD_ID" --json
```

`stop` leaves the launch agent file and private receipt history; `install` can
restart the same binding. `uninstall` removes the launch agent and binding but
retains the private receipts for review. A changed binding requires owner
review. Keep the packaged files at a stable installed path. Package
`listener.py`, `host_queue.py`, `ipc.py`, and reviewed copies of `dbond.py`
and `owner.py` in `Contents/Resources/agent-installers/codex-session/`.

The disposable test is `python3 -m unittest discover -s adapters/codex-session
-p 'test_*.py'`. It does not contact a real broker or Codex host.
