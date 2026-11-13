# Claude Code session listener reminder

The Claude Code plugin registers `hooks/hooks.json` as a `SessionStart` hook.
Its bundled script checks the host-provided `session_id` through the
owner-private `dbond session-binding SESSION_ID --client claude` lookup. It
continues only when there is one exact Claude binding and `dbond session-status
ALIAS` confirms the same service ID with `pending_listener`. Unrelated chats,
ambiguous bindings, active listeners, paused pairings and unavailable status
produce no output. No credentials or message bodies enter hook context.

For that exact session, the hook tells Claude on its **next actual turn** to
use its built-in Monitor on `dbond session-listen ALIAS` with a 30-minute
timeout, then verify `dbond session-status ALIAS` reports
`subscriber_present`. When Monitor exits, the active host must recheck and
re-arm it. Stream presence is not proof that the agent read or answered a
message. Separately reported wake verification is evidence of an authenticated
host turn, not proof of unsolicited idle-chat wake.

Packaging must include:

- `adapters/claude/plugin/hooks/hooks.json`
- `adapters/claude/session-hooks/session_start.py` copied to
  `adapters/claude/plugin/hooks/session_start.py`
- `adapters/claude/plugin/server/dbond.py` with `session-binding` and
  `session-status`

Claude Code runs hooks in Desktop Code sessions as well as terminal sessions,
but the installed Desktop-owned session must actually load this plugin and
its hook. Verify with `/hooks` and a controlled resume. The standalone
Desktop MCPB only supplies MCP tools; it does not install this Claude Code
plugin hook or edit user settings.

`SessionStart` adds context before the first model response after startup or
resume. It cannot create an interactive idle turn, renew an expired Monitor
without an active host turn, or wake a closed chat. `initialUserMessage` is
only a turn starter in noninteractive `-p` mode.

Run source checks:

```sh
python3 -m unittest discover -s adapters/claude/session-hooks -p 'test_*.py'
```

Source: [Claude Code hooks reference](https://code.claude.com/docs/en/hooks).
