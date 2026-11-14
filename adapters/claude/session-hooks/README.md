# Restore a Claude session listener

The D-Bond Claude Code plugin runs its SessionStart hook on startup and resume. It checks the exact Claude session UUID and its saved D-Bond pairing. On a supported Mac session, the receiver uses the chat's native inbox for direct messages and addressed Dot pushes. Resuming the chat reuses its pairing and cursors. A host that admitted the native Claude channel can receive messages through that channel.

An unpaired chat needs no listener. If neither native route is available, Claude can use its built-in Monitor to run the command returned by registration. That watch has a host deadline. A closed Claude session must reopen before it can process messages.

The standalone Claude Desktop extension provides callable tools. It does not install Claude Code hooks.

[Claude Code hook lifecycle](https://code.claude.com/docs/en/hooks) · [Session inbox](https://code.claude.com/docs/en/cross-session-messaging#the-sessions-inbox-socket)
