# Install and connect D-Bond

On a Mac with Swift 5.9+ and macOS 13 or later, run the following from the source checkout:

```sh
bash scripts/install_app.sh
```

Quit an older running D-Bond before upgrading. If it was already listed under **System Settings → General → Login Items & Extensions → Open at Login**, remove that older entry before installing; the installer starts one per-user launchd job instead. The job opens D-Bond at login and restarts it after an abnormal exit. **Quit D-Bond** exits normally and keeps it stopped until the next login. The installer does not remove pairings, credentials or receipts.

The app runs in the menu bar. Opening it again brings up **Connections**. A fresh installation can test **Local demo echo** immediately. To reach your actual Dot you also need your own compatible Dot Bridge Transport and its single native `d-bond` subscription. Installing the Mac app alone doesn't create that account connection.

**Connect agents** opens the packaged Claude Desktop extension, installs the local Claude Code channel plugin or Codex Local Control through their host CLIs when you click the relevant button, and copies a generic stdio MCP configuration. Claude Desktop still asks you to approve and enable its extension. Claude Code still requires channel opt-in in the intended session. Codex Local Control includes direct and Dot messaging in the same plugin; a new chat may be needed for discovery. An older separately registered `d-bond-agent` server is left unchanged. The setup window lists only paired, unpaused agents with an active direct listener for its optional **Send setup note** action. That action records delivery to the listener, not an AI turn or answer.

On an agent connection row, **Connect session…** shows the direct listener, Dot stream, and wake-check state separately for that exact connection ID. Registration checks the paired session once. The session acknowledges the wake message by its broker sequence. D-Bond keeps the receipt across restarts and shows live listener status separately. An explicit retry starts a fresh check. Codex registration belongs to the exact chat through Local Control `enroll_agent`; the per-chat host listener queues event pointers, and the chat must still fetch and answer messages. The Claude `session-listen` command shown in the dialog is a repair path for its exact host Monitor, which expires with the session.

## App settings

Choose **Settings…** from the D-Bond menu or press **⌘,** while a D-Bond window is active. **Start D-Bond at login** controls automatic startup on this Mac. **Open Connections at launch** opens the Connections window each time; first-time setup opens it automatically. The menu and Settings show the installed version and build.

## Connect a service

2. Select the destination. For a browser extension that calls the broker directly, enter the exact extension ID from Chrome. Copy the endpoint and one-time secret into that service’s local settings. Existing connections have a **Setup** button with instructions. A pairing is saved even when the service is offline; in the window, “connected” means a receiver is listening.
3. Your service posts a stable event ID and a JSON payload to its loopback endpoint. For replies, it opens the authenticated event stream. Its integration decides what to do when a reply arrives. A public webhook uses an authenticated receiver in front of this loopback connection.


## Connect Herald

In **Open Connections… → Add Connection…**, choose **Herald notification** and name it **Herald / Notifications**. When creating it, leave **Allow paired services to ask Dot to notify me** checked if you want this standing permission. If the connection already exists, reuse it and edit its **Rules → Plain notifications from paired services**. The internal rule ID is `general-notifications`. In Herald, open **Settings → General → D-Bond** to check the connection.

From a paired Codex or Claude session, ask “Have Dot send me a test notification through Herald.” The equivalent CLI command is:

```sh
dbond ask YOUR_SESSION_ALIAS "Send me a short test notification" --to "Herald / Notifications"
```

D-Bond identifies the sender and the Herald destination to Dot. Dot writes the title and body; D-Bond forwards that reply to Herald on the Mac. You do not have to repeat the request in Dot chat. The session’s reply tells you what Dot wrote. **Herald → History** confirms the notification reached Herald. For interactive questions with buttons, use Herald’s reply-capable connector.

## Attach your SOP once

In **Connections**, choose **Rules** next to a service. Select a rule in the list to edit its Markdown. **Add** starts a new rule; the arrows change the order Dot reads them. Use the toolbar for headings, bold text, lists, checklists, or code, and **Preview** to see the formatted instructions. **Save rules** (⌘S) applies all your edits together. If another editor saved first, your draft stays open: copy it before you reload the saved rules. The owner can change a connection’s rules without re-pairing, editing the webhook body, or changing another service. Each accepted service request carries the rule version it had at intake; retries of the same event retain it.



## Inspect a delivery


**Events** shows the ten most recent entries and updates as messages arrive. Each row shows a short preview, the other connection, direction, delivery state, and local time to the second. The preview takes up to two sentences or 210 characters. A fenced code block shows its language and first content line. A correlated Dot answer shows its own preview and the local time the broker observed the reply. **View complete log** opens the retained history in pages of 50, with event IDs, reply references, and recorded stages. Pages stay in place as new messages arrive. **Copy shown page** copies the visible page. Deleting a connection removes its retained history.

Direct agent messages appear in both participants' Events windows. Each entry says **Received from** or **Sent to** and names the other connection. A message sent to the same connection is labeled as such. It shows the broker sequence and, for a direct reply, **In reply to event** so a PONG can be tied to its PING. **Stored by D-Bond for recipient** means the broker persisted the message for replay; it does not prove that the recipient's AI session read it. An event ID can occur in multiple directions or from different senders. For a single trace, use the direction and exact counterpart ID shown by `dbond trace` or Local Control to select the intended row.

- A **queued** timestamp means the local Mac stored the event. **dot_stored** means the transport accepted it into storage.
- **dot_acknowledged** and **dot_replied** are separate. The displayed local times show when D-Bond observed those changes; they do not establish when a person saw a banner or a second app acted.
- An empty stage is unknown, not zero elapsed time. Older events may have no timestamp history. Reconcile an uncertain send using the original event ID.


## After an interruption

The service keeps the same endpoint, credential and event ID across restarts. Reopen the receipt stream with the last sequence your service durably handled. If a POST response is lost, resend the same ID and unchanged payload; creating a new ID starts a new message. The broker retries temporary transport failures with backoff. It checks the native request store before sending again. A rejected or unsafe-to-replay message has an explicit receipt. A paused or deleted link does not silently become active.

The native Dot subscription and its account grant are separate from your service pairing. The host can renew them automatically; an owner revocation, a disabled grant, or a missed host renewal can still prevent a wake. While Dot is offline, its stored messages are subject to the native host's expiry. The broker can restart the Mac app and its observer; it cannot wake a powered-off computer or guarantee Dot's response time.

## Service icons and settings


[Direct agent messages](../adapters/direct/README.md) documents the CLI commands, stdio tools, streaming inbox, and which setup steps need an open client session.

In **Agent Setup**, use **Check again** after changing a host setting. The Codex row shows Local Control and whether direct messaging is included; an older standalone direct server appears as a legacy installation. Claude Code and Claude Desktop remain separate installations. A disabled or conflicting configuration is shown for review. From any updated D-Bond MCP adapter, call `local_status` to check whether the Mac app is installed and its local bridge is reachable; this does not register a service or establish that an agent is listening.
