# Claude adapter

Build the local Claude Desktop extension with `python3 adapters/claude/build.py`,
then install `dist/d-bond-claude.mcpb` through Claude Desktop's local extension
settings. Keep D-Bond running. The extension provides enroll, peers, ask,
wait_for_push, and answer tools. It can receive a push only while
`wait_for_push` is running; it does not wake a closed chat.

For Claude Code, build first and load `adapters/claude/plugin` as a local plugin.
Configure a unique pairing alias for the open session and use its `enroll`
tool. An addressed push appears as a channel event; answer with its source ID.
Channel support and permission depend on the installed Claude Code version.

The adapters use D-Bond's local pairing store. Dot delivery additionally needs
the separately installed and authenticated Dot Bridge Transport host described
in the repository README. The extension bundle contains no pairing credential.
