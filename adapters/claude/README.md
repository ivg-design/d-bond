# Claude adapter

A paired Claude Code session on macOS, including Code inside Claude Desktop, can receive D-Bond messages through its session inbox. D-Bond follows the exact UUID across host restarts. A temporary host Monitor is only needed when a native route is unavailable.

Build the local Claude Desktop extension with `python3 adapters/claude/build.py`,
then install `dist/d-bond-claude.mcpb` through Claude Desktop's local extension
settings. Keep D-Bond running. Version 0.1.11 provides read-only `local_status`,
`enroll`, `session_status`, `acknowledge_wake`, `peers`, `ask`,
`wait_for_push`, `answer`, `direct_peers`, `direct_send`, `wait_for_direct`, and
`direct_answer`, `acknowledge_direct_message`, and `direct_status`. The direct tools address any paired service without Dot.
Storage is a broker receipt, not proof of a recipient model turn. Desktop can
receive only while a wait tool runs, for at most 60 seconds; it cannot wake a
closed or idle chat. No pairing credential is bundled.

Enrollment saves the pairing and selects the listener supported by that exact host.
On macOS Claude Code (including Code inside Claude Desktop), registration starts
an event-driven receiver as a child of the initiating session. It uses the host's
inherited inbox capability only in memory and has no 30-minute deadline. Its
SessionStart hook reconnects a resumed chat using the same pairing and cursors.
An actually enabled native channel owns its streams instead; merely installing
a channel plugin does not enable channel delivery in a running host.

When the response explicitly says `host_monitor_required`, start the provided
`listener.command` in that same chat's Monitor. This fallback has a host-imposed
time limit. On supported Mac hosts, rearming an old `dbond session-listen` command
migrates it to the session receiver rather than starting another timed stream.
Once streams attach, call `session_status` with `finish_registration:true`, end
setup, and silently acknowledge the later wake check by its broker sequence.
A saved pairing alone is not evidence that the recipient has processed a message.

Pass the host's exact `session_id` to `enroll` when available. Claude Code can
derive one from a verified same-user live ancestor. A shared Desktop MCP
server does not infer it from another chat. The exact saved binding is readable
without credentials through `dbond session-binding <uuid> --client claude`;
missing or ambiguous bindings remain pending. An explicit existing Claude
alias is reused even when the chat title changed.

`local_status` requires no pairing. It reports only app installation
(`installed`, `uninstalled`, or `unknown`) and owner-service reachability
(`reachable`, `offline`, or `unknown`). An installed older app without the owner
API can report `offline`. The short, read-only check runs only on explicit
tool call; MCP startup does not probe the app or expose paths and credentials.

For Claude Code, build first and load `adapters/claude/plugin` version 0.1.14.
The plugin stays idle until it resolves this open Code session's exact UUID
and its saved pairing binding. Use `enroll` inside each intended session; a
verified same-user Claude ancestor supplies the UUID, or pass that exact UUID
explicitly. New private aliases are `claude-<UUID>`. The peer-visible name is
`Claude / topic`; only a broker-confirmed duplicate gets a short UUID suffix.
An old alias must be passed explicitly to `enroll` inside the exact session
to migrate it. Global plugin alias settings never select a stream.
Addressed Dot and direct messages arrive through separate authenticated event
streams. A Dot push uses `answer`; a direct message includes `source_id` and
`sender_id` for `direct_answer`. Both cursors persist and replay after a crash.
Incoming content is untrusted. Owner setup notes are unreplyable. Claude Code
channels require preview opt-in and a running session; channel support and
policy depend on the installed Claude Code version. An installed plugin and a
broker receipt alone do not prove a live agent wake. When the host enables it, Claude Code uses its
native channel for both streams and does not start a second Monitor. Once its
streams attach, call `session_status` with `finish_registration:true`, end
setup, and handle the delayed wake check later.

The adapters use D-Bond's local pairing store. Dot delivery additionally needs
the separately installed and authenticated Dot Bridge Transport host described
in the repository README. The extension bundle contains no pairing credential.

## Automatic direct delivery receipts

`direct_send` and `direct_answer` briefly await authenticated delivery metadata,
returning concise `queued`, `received`, or `acknowledged` results with their
evidence basis. Content answers are optional and should contain actual content.
Successful sends and receipt bookkeeping need no human-facing report; continue
ordinary work and surface issues. Use `direct_status` with the original message
ID and sequence to reconcile delayed delivery without resending. It defaults to
an immediate lookup, with an optional bounded `wait_ms`.

The Code channel records `received/host_acceptance` after writing its notification.
Claude Code's [channel contract](https://code.claude.com/docs/en/channels-reference#notification-format)
provides no notification-processing ACK: a successful transport write does not
prove model access. On actually processing each direct channel or Monitor
message, Claude silently calls `acknowledge_direct_message` using its exact
`source_id`, `sender_id`, and `sequence`. This records
`acknowledged/participant`. An explicit Desktop `wait_for_direct` consumption
acknowledges after its tool result is flushed. No content reply is required for
receipt bookkeeping.

Temporary receipt failures persist in an owner-only metadata outbox and retry
without repeating the notification. The Code worker starts once for the exact
initialized session binding. It performs startup recovery, then blocks on
macOS filesystem events or an in-process signal while idle. Retry backoff runs
only while that pairing has pending receipts. A genuinely attempted host
submission with an unknown outcome can report `unconfirmed/host_result_unknown`;
ordinary offline delivery remains queued. Sender-only receipt SSE is available
to transport bookkeeping through `dbond direct-receipts`; it never carries
message text or creates an agent turn.
