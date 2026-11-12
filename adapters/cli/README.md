# D-Bond universal CLI

From a running local Claude session, use `dbond enroll <alias>` without a fixed `--name`. When the CLI can verify a Claude ancestor PID and its start time against the local Claude session record, the menu uses that chat’s actual title. For a client with no identifiable local ancestor use an explicit host title/ID. The pairing alias is your short command name; it need not be the display title.

`dbond.py` is a small Python 3 sender and receiver for any process that can run
a local command: Codex, Claude, shell scripts, or another local agent. It talks
only to the D-Bond loopback endpoint in a pairing; it does not call DotBridge,
Slack, Beeper, Herald, or other services directly.

## Address a connection through Dot

Use `dbond peers <your-alias>` to list the registered names and IDs.
For example, a paired Codex chat can use
`dbond ask dbond-design --to "Claude / Design review" "Which page are you reviewing?"`.
D-Bond resolves the recipient from its connection list and delivers Dot’s reply to both the caller and recipient. A plain
`dbond ask dbond-design "Which connections are paired?"` asks Dot itself.
The addressed chat receives the push through its running listener.

## Link a pairing

From this directory:

```sh
./dbond.py link render --file /path/to/pairing.json
```

The JSON must contain `endpoint` and `secret` as shown by D-Bond. `--file -`
reads JSON from stdin. With no `--file`, an interactive terminal asks for the
endpoint and prompts for the credential without echoing it. The local store is
`~/Library/Application Support/D-Bond CLI`, mode 0700, with owner-only pairing
files. Symlinked paths and pairing files are rejected. Output does not include
the credential.

When the D-Bond menu app is running, create a fresh pairing directly through its
owner-only same-user Unix socket:

```sh
dbond enroll render --service Renderer --topic "Render watcher"
```

The menu and Dot both see `Renderer / Render watcher`; the shell uses `render`.
Enrollment refuses an alias
that already exists, asks the app to create a Dot-destination pairing, then saves
the returned credential in the private local store. It does not read secrets
from another pairing. To expose the command on `PATH`, symlink this executable
to `~/.local/bin/dbond`.

For a Claude or Codex session, provide its exact UUID and a title reported by
the host:

```sh
dbond enroll review --client claude --session-id "<exact-session-uuid>" --title "Review Dot bridge feasibility"
dbond enroll review --client codex --session-id "<exact-session-uuid>" --title "Review Dot bridge feasibility"
```

Claude can look up a missing title only against that exact UUID: the local
transcript's `custom-title` takes precedence over `ai-title`, then an exact
`cliSessionId` match in Claude Desktop's session metadata. It reads only title
metadata, never prompt text or other session transcripts. Codex title lookup is
not inferred from its local cache because that can contain raw prompt text;
provide `--title` from the active host, or the menu uses your local alias as the
topic. Without the actual ID, the CLI cannot identify which chat is current and
does not guess from other chats. Explicit `--title` is preferred for both.

## Ask Dot and receive its receipt

```sh
./dbond.py ask render "Is the render ready?" --timeout 120
```

The command opens the pairing's authenticated SSE stream from sequence 0 before
posting the question, so replay can recover a receipt that arrives immediately
or after a client restart. It prints the unique event ID and, once received,
the durable Dot Bridge request ID. `dot_stored` means the bridge accepted the
request into storage; only `dot_acknowledged` proves Dot explicitly acknowledged
it, and `dot_replied` contains Dot's final answer. If the deadline expires,
the command exits 3 and prints the latest observed state and IDs. Reconcile that
same event before retrying; pass `--event-id` to reuse an ID when intentionally
retrying an uncertain submission.

## Receive and answer Dot pushes

Keep a listener running in the process that should receive pushes:

```sh
./dbond.py listen render
```

The listener uses SSE, resumes after its last locally saved sequence, and prints
each addressed Dot push as JSON. Answer only a push recorded for that pairing:

```sh
./dbond.py answer render @dot:91 "The render finished successfully."
```

This submits the payload `{ "kind": "service.reply", "in_reply_to":
"@dot:91", "text": "..." }`. The local app's native bridge accepts incoming
pushes only when the bridge data identifies caller `d-bond`, the addressed
conversation is this service's UUID, and the push channel is `d-bond`; this is
the origin/routing check performed in `scripts/native_link.py`. The Dot reply
then travels as a new service event. `answer` waits for its explicit ACK or
reply, prints the event/request IDs, and exits 3 if only a stored receipt is
available by the deadline; set `--timeout` to change the default 120 seconds.

An SSE listener can receive a push while its process is running. It does not
wake an idle or ended Codex/Claude chat by itself; the invoking environment must
keep the listener alive and route its output to an active session. A stored
message or a `dot_push` receipt is not proof of a human-visible notification.

## Local fixture

Run the focused protocol fixture without contacting D-Bond or Dot:

```sh
python3 -m unittest adapters.cli.test_dbond -v
```
# Local event traces

Use `dbond trace <connection-uuid> [event-id]` to read recent traces from the running Mac app. Add `--limit 1` to inspect only the newest event. Displayed times use the Mac’s current timezone and omit fractions of a second; the stored ledger and owner-socket protocol keep precise UTC timestamps. Existing whole-second timestamps are supported.
