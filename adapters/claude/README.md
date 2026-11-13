# Claude adapter

Build the local Claude Desktop extension with `python3 adapters/claude/build.py`,
then install `dist/d-bond-claude.mcpb` through Claude Desktop's local extension
settings. Keep D-Bond running. Version 0.1.8 provides read-only `local_status`,
`enroll`, `session_status`, `acknowledge_wake`, `peers`, `ask`,
`wait_for_push`, `answer`, `direct_peers`, `direct_send`, `wait_for_direct`, and
`direct_answer`. The direct tools address any paired service without Dot.
Storage is a broker receipt, not proof of a recipient model turn. Desktop can
receive only while a wait tool runs, for at most 60 seconds; it cannot wake a
closed or idle chat. No pairing credential is bundled.

After `enroll` in Claude Desktop, the pairing is saved but incoming wake is
pending. In the same turn, start the host's native Monitor on the returned
`listener.command` (`dbond session-listen <exact alias>`), then call
`session_status` with `finish_registration:true` after its streams attach and
end setup immediately. The finish action schedules one delayed proof;
ordinary status is read-only and repeat finish is broker-idempotent. The combined listener receives direct agent messages and
addressed Dot pushes. The delayed owner wake check requires
`acknowledge_wake` with the received sequence in that later turn; only
`wakeState:verified` confirms an authenticated connection ACK while its stream
was active, not an independently verified model turn. The MCP server does not start a detached
listener or claim a closed chat can wake itself.

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

For Claude Code, build first and load `adapters/claude/plugin` version 0.1.10.
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
broker receipt alone do not prove a live agent wake. Claude Code uses its
native channel for both streams and does not start a second Monitor. Once its
streams attach, call `session_status` with `finish_registration:true`, end
setup, and handle the delayed wake check later.

The adapters use D-Bond's local pairing store. Dot delivery additionally needs
the separately installed and authenticated Dot Bridge Transport host described
in the repository README. The extension bundle contains no pairing credential.
