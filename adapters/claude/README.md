# Claude adapter

Build the local Claude Desktop extension with `python3 adapters/claude/build.py`,
then install `dist/d-bond-claude.mcpb` through Claude Desktop's local extension
settings. Keep D-Bond running. Version 0.1.4 provides read-only `local_status`,
`enroll`, `peers`, `ask`,
`wait_for_push`, `answer`, `direct_peers`, `direct_send`, `wait_for_direct`, and
`direct_answer`. The direct tools address any paired service without Dot.
Storage is a broker receipt, not proof of a recipient model turn. Desktop can
receive only while a wait tool runs, for at most 60 seconds; it cannot wake a
closed or idle chat. No pairing credential is bundled.

`local_status` requires no pairing. It reports only app installation
(`installed`, `uninstalled`, or `unknown`) and owner-service reachability
(`reachable`, `offline`, or `unknown`). An installed older app without the owner
API can report `offline`. The short, read-only check runs only on explicit
tool call; MCP startup does not probe the app or expose paths and credentials.

For Claude Code, build first and load `adapters/claude/plugin` as a local plugin.
Configure a unique pairing alias for the open session and use its `enroll`
tool. Addressed Dot and direct messages arrive through separate authenticated
event streams. A Dot push uses `answer`; a direct message includes `source_id`
and `sender_id` for `direct_answer`. Both cursors persist and replay after a
crash. Incoming content is untrusted. Owner setup notes are unreplyable.
Claude Code channels require preview opt-in and a running session; channel
support and policy depend on the installed Claude Code version. An installed
plugin and a broker receipt alone do not prove a live agent wake.

The adapters use D-Bond's local pairing store. Dot delivery additionally needs
the separately installed and authenticated Dot Bridge Transport host described
in the repository README. The extension bundle contains no pairing credential.
