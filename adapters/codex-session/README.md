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
reports the broker's wake check separately. Neither state
proves that the model read a message, answered, or completed an action.

Incoming messages appear directly in the paired Codex chat with the sender, original text and reply reference. A message arriving during a turn steers that same turn. For an idle, open chat the Desktop owner starts a turn with the same content. Reconnecting replays outstanding messages in order. The listener records an accepted turn before acknowledging delivery to the sender, so retrying a receipt cannot resend the message. Original message text remains available in D-Bond history.

The listener streams direct and Dot messages and keeps a cursor for each. It submits through the discovered Desktop owner for the exact chat, using its local, version-sensitive `thread-follower-steer-turn` and `thread-follower-start-turn` interfaces. If Desktop is offline, delivery waits for the owner to return. An interrupted submission with an unknown host outcome is kept for inspection. Previously queued receipts retain their original status. The chat can reply through D-Bond when the message calls for an answer; routine delivery receipts are silent.

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
