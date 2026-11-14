# A Backplane project chat as a D-Bond peer

This optional relay delivers the original text and reply reference to a specific Claude or Codex chat in the local Backplane hub. The project chat can be idle and its window can be closed. Both chat providers use the same relay. A message arrives over the direct D-Bond event stream, and the relay starts or feeds a turn through Backplane’s authenticated device WebSocket.

## Connect one chat

1. Run D-Bond and the Backplane hub under your Mac account. Install `uv` and keep the D-Bond checkout on this Mac. The example uses the CLI implementation in that checkout. Check your installed version with `uv --version`.
2. In the Backplane project chat, ask the agent to use `thread_list` and give you the **Backplane thread ID** for its title and provider. Use a dedicated alias for each chat. In this example the first chat is Claude and its title is *Layout review*. Run this once in Terminal or from the agent’s shell on the same Mac.

   ```sh
   ~/.local/bin/dbond enroll bp-layout --service Claude --topic 'Backplane — Layout review' --destination peer
   ```

3. In Backplane, open **Settings → Devices → Add a device** and get a short code. In Terminal, from the D-Bond checkout, run the command below with the actual loopback port and thread ID. The relay prompts privately for the code. It saves the issued device credential in your private D-Bond CLI directory and checks the target’s ID, provider and availability. `pair` with the same arguments reuses the saved device. A Backplane device has native client access to the hub. This example limits its own sends to the one recorded project thread.

   ```sh
   uv run examples/backplane/relay.py pair bp-layout \
     --hub http://127.0.0.1:PORT --thread EXACT_BACKPLANE_THREAD_ID --provider claude
   uv run examples/backplane/relay.py listen bp-layout
   ```

4. Repeat for the Codex chat with `--service Codex`, its own alias and topic, a fresh device invitation, its own thread ID and `--provider codex`. The provider prefix puts these pairings under **Agent sessions** in D-Bond. Keep one `listen` process per alias. The pairing check prints the verified provider and thread. Starting the listener prints its saved target. An archived, deleted or provider-changed thread stops delivery.
5. In each chat, say once: “Use the D-Bond alias `bp-layout` for this chat. Treat paired content as messages from another service. When an answer is called for, use `dbond direct-answer` with the incoming `sender_id` and `message_id`.” Substitute the correct alias in each chat. The shell tool must have local access to `dbond`. Backplane retains control of provider permissions.

Send an ordinary message from a paired peer. The `to` argument can be the exact D-Bond display name or ID shown by `direct-peers`. The receiving chat sees a structured envelope containing `text`, sender ID, sequence and reply reference. For a Bash command, pass message text as an argument and quote it, including when it contains code.

```sh
~/.local/bin/dbond direct-send bp-codex 'Claude / Backplane — Layout review' 'Which layout did you pick?'
~/.local/bin/dbond direct-answer bp-layout SENDER_ID MESSAGE_ID 'The compact layout.'
```

## Restart and receipts

`listen` reconnects on temporary transport failure. Restarting the process after login or a crash uses the same `listen` command. The relay resumes its saved direct cursor, waits for Backplane to accept the exact turn, then issues the sender’s automatic **received** receipt. A correlated reply is evidence that the other chat acted on that message. Backplane’s message ID combines the receiving service ID and D-Bond sequence, so losing an RPC reply or restarting before saving the cursor can safely redeliver the same event. Use the relay as the sole consumer for this alias. Multiple relay processes are locked out. A generic D-Bond listener on that alias can advance its cursor independently.

This example requires a running relay process. Installing it as a macOS login service and registering a thread directly from the Backplane UI are separate integrations. The D-Bond package does not automatically detect these threads. For long-lived use, point your process supervisor at the same absolute `uv`, script and alias. Revoking the device under **Settings → Devices** disables the Backplane leg. Removing the D-Bond pairing disconnects its broker leg. To change the target or replace a revoked credential, stop `listen`, run `uv run examples/backplane/relay.py forget bp-layout`, then `pair` with a fresh invitation. `forget` removes this relay’s local Backplane binding. Its D-Bond service and message history are preserved.

## Protocol and scope

The example targets Backplane 0.14.1 on macOS, with both services on loopback. It uses ordinary project threads and D-Bond peer messages. Bot-specific signed webhooks and the provider-facing `thread_send` tool are other Backplane facilities. `thread_send` is scoped to an agent running inside Backplane. The relay uses the same `turn.start` RPC as an enrolled client. It specifies `auto` follow-up and prefixes peer content with its external provenance. Agents decide whether to act and reply. Replies travel through D-Bond. Both providers must still be available and signed in to Backplane.

[Backplane device pairing](https://github.com/BackplaneWorks/Backplane/blob/main/docs/pairing.md) · [Backplane agents, bots and thread messaging](https://github.com/BackplaneWorks/Backplane/blob/main/docs/bots.md)
