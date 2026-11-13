# Install and connect D-Bond

On a Mac with Swift 5.9+ and macOS 13 or later, run the following from the source checkout:

```sh
bash scripts/install_app.sh
```

Quit an older running D-Bond before upgrading. If it was already listed under **System Settings → General → Login Items & Extensions → Open at Login**, remove that older entry before installing; the installer starts one per-user launchd job instead. The job opens D-Bond at login and restarts it after an abnormal exit. **Quit D-Bond** exits normally and keeps it stopped until the next login. **Start at Login** in the app menu enables or disables the installed job. The installer does not remove pairings, credentials or receipts.

The app runs in the menu bar. Opening it again brings up **Connections**. A fresh installation can test **Local demo echo** immediately. To reach your actual Dot you also need your own compatible Dot Bridge Transport and its single native `d-bond` subscription. Installing the Mac app alone doesn't create that account connection.

**Connect agents** opens the packaged Claude Desktop extension, installs the local Claude Code channel plugin or Codex tools through their host CLIs when you click the relevant button, and copies a generic stdio MCP configuration. Claude Desktop still asks you to approve and enable its extension. Claude Code still requires channel opt-in in the intended session. The Codex install adds both local controls and direct messaging tools; a new chat may be needed for discovery. The setup window lists only paired, unpaused agents with an active direct listener for its optional **Send setup note** action. That action records delivery to the listener, not an AI turn or answer. For an unpaired or idle chat, copy the note and paste it into the exact chat yourself.

## Connect a service

2. Select the destination. For a browser extension that calls the broker directly, enter the exact extension ID from Chrome. Copy the endpoint and one-time secret into that service’s local settings. Existing connections have a **Setup** button with instructions. A pairing is saved even when the service is offline; in the window, “connected” means a receiver is listening.
3. Your service posts a stable event ID and a JSON payload to its loopback endpoint. For replies, it opens the authenticated event stream. Its integration decides what to do when a reply arrives. A public webhook uses an authenticated receiver in front of this loopback connection.


## Attach your SOP once

In **Connections**, choose **Rules** next to a service. Select a rule in the list to edit its Markdown. **Add** starts a new rule; the arrows change the order Dot reads them. Use the toolbar for headings, bold text, lists, checklists, or code, and **Preview** to see the formatted instructions. **Save rules** (⌘S) applies all your edits together. If another editor saved first, your draft stays open: copy it before you reload the saved rules. The owner can change a connection’s rules without re-pairing, editing the webhook body, or changing another service. Each accepted service request carries the rule version it had at intake; retries of the same event retain it.



## Inspect a delivery


The **Events** window opens with the newest ten events for that connection. Click **View complete log** to page through every event retained in its local ledger, 50 at a time. **Older events** and **Newer events** move through the captured history; new arrivals do not shift a page while you browse. The log shows each event's first recorded time, current state, rule version when available, and every recorded stage with its basis. Times are displayed in this Mac's timezone to the second. **Copy shown page** copies only the visible page. Deleting the connection removes its retained history.

- A **queued** timestamp means the local Mac stored the event. **dot_stored** means the transport accepted it into storage.
- **dot_acknowledged** and **dot_replied** are separate. The displayed local times show when D-Bond observed those changes; they do not establish when a person saw a banner or a second app acted.
- An empty stage is unknown, not zero elapsed time. Older events may have no timestamp history. Reconcile an uncertain send using the original event ID.


## After an interruption

The service keeps the same endpoint, credential and event ID across restarts. Reopen the receipt stream with the last sequence your service durably handled. If a POST response is lost, resend the same ID and unchanged payload; creating a new ID starts a new message. The broker retries temporary transport failures with backoff. It checks the native request store before sending again. A rejected or unsafe-to-replay message has an explicit receipt. A paused or deleted link does not silently become active.

The native Dot subscription and its account grant are separate from your service pairing. The host can renew them automatically; an owner revocation, a disabled grant, or a missed host renewal can still prevent a wake. While Dot is offline, its stored messages are subject to the native host's expiry. The broker can restart the Mac app and its observer; it cannot wake a powered-off computer or guarantee Dot's response time.

## Service icons and settings


[Direct agent messages](../adapters/direct/README.md) documents the CLI commands, stdio tools, streaming inbox, and which setup steps need an open client session.

In **Agent Setup**, use **Check again** after changing a host setting. Codex Local Control and direct messaging have separate statuses. Claude Code and Claude Desktop are separate installations. A missing component can be added without recreating a pairing. A disabled or conflicting configuration is shown for review. From any updated D-Bond MCP adapter, call `local_status` to check whether the Mac app is installed and its local bridge is reachable; this does not register a service or establish that an agent is listening.
