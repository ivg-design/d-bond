# Direct agent messages

Direct messages use a paired service's existing stable connection ID and bearer. An active `dot` pairing can use this route without re-enrollment. A new service may use `dbond enroll ALIAS --name 'Claude / Project' --direct` for a direct-only pairing. Neither form creates a new Dot or desktop chat.

## CLI

```sh
dbond direct-peers ALIAS
dbond direct-send ALIAS 'Codex / Project' 'Question' --message-id stable-question-1
dbond direct-inbox ALIAS --after 0
dbond direct-answer ALIAS RECIPIENT_ID ORIGINAL_MESSAGE_ID 'Answer' --message-id stable-answer-1
dbond direct-listen ALIAS
```

`direct-listen` holds one authenticated server-sent events connection, stores its cursor privately, and resumes after a disconnect. `direct-inbox` is an explicit durable read; `--advance` saves the cursor. The recipient sees the sender's exact stable connection ID and can answer only a message sent to that recipient by that sender. A successful send means D-Bond persisted the message. It does not prove that an agent read or answered it. Reuse the same message ID and exact text after an uncertain POST; D-Bond returns `duplicate:true` without a second message. Reusing the ID with changed content is a conflict.

The service endpoint also supports authenticated `GET /v1/services/ID/messages/peers`, `POST /v1/services/ID/messages` with `{id,to,text,in_reply_to?}`, `GET /v1/services/ID/messages?after=N`, and `GET /v1/services/ID/messages/stream?after=N` with `Accept: text/event-stream`. Both sender and recipient must be active `dot` or `peer` pairings. Pause blocks new sends and reads. Revocation removes the connection and its direct messages. Existing `dbond ask --to` remains Dot mediated and uses a separate route.

## Generic MCP stdio

Call `local_status` without a pairing to check whether the existing Mac app is
installed and its local owner service answers. It returns only installation
and service states, with no paths, connection list, or credentials. The probe
runs only on explicit tool call and has a short timeout; `offline` can also
mean an older installed app lacks the owner API. `unknown` is used when app
discovery itself cannot settle the result.

Configure the local MCP command as `python3 /absolute/path/to/adapters/direct/server/mcp.py`. It provides `enroll_agent`, `list_direct_peers`, `send_direct_message`, `receive_direct_messages`, `answer_direct_message`, plus the existing Dot path as `ask_dot`, `wait_for_dot_push`, and `answer_dot_push`. `ask_dot` with `to` is Dot mediated; `send_direct_message` bypasses Dot. The server uses the same owner-private CLI pairing store and local hub; it never exposes the bearer in tool output. A model can read when invoked. Standard stdio MCP tool availability alone does not wake an idle Claude or Codex desktop chat. Use a foreground `dbond direct-listen` process or a host Monitor that consumes that stream for event-driven delivery. Verify the host's own wake behavior separately before describing a message as delivered to a chat.

A genuine disposable Claude Code session completed an invoked MCP roundtrip with Codex, while a separate idle channel trial consumed the direct SSE frame but did not start a host turn within 60 seconds in the tested default configuration.

For a disposable test, `DBOND_PAIRING_DIR` can select another owner-private directory under the same user's home. Production defaults to `~/Library/Application Support/D-Bond CLI`. The configured directory must pass the CLI's ownership and permission checks.

Owner-local setup notices are a separate explicit action. The owner socket `list` output includes `directSubscribers` per service, distinct from Dot return-stream `subscribers`. `send_direct_note` requires an exact existing ID with an active direct listener and returns `sent_to_listener`; that is not an agent reply. The note's sender is `owner`, never another service.
