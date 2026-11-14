# Install and connect D-Bond

For an Apple silicon Mac, download the app from [GitHub Releases](https://github.com/ivg-design/d-bond/releases/latest), move it to Applications and choose **Connectors…**. The download includes the runtime. For a source installation, use Swift 5.9+, Python 3.11+, and macOS 13 or later:

```sh
bash scripts/install_app.sh
```

Quit an older running D-Bond before upgrading. If it was already listed under **System Settings → General → Login Items & Extensions → Open at Login**, remove that older entry before installing; the installer starts one per-user launchd job instead. The job opens D-Bond at login and restarts it after an abnormal exit. **Quit D-Bond** exits normally and keeps it stopped until the next login. The installer does not remove pairings, credentials or receipts.

Open **Connections** from the menu bar to add or inspect a pairing. On a fresh installation you can try a local demo immediately. Choose **Connectors… → Configure Dot Connector…** to connect your own Dot through the bundled MCP host. [Dot setup](../README.md#connect-your-existing-dot)

**Connectors…** opens the packaged Claude Desktop extension, installs the local Claude Code channel plugin or Codex Local Control through their host CLIs when you click the relevant button, and copies a generic stdio MCP configuration. Claude Desktop still asks you to approve and enable its extension. Claude Code still requires channel opt-in in the intended session. Codex Local Control includes direct and Dot messaging in the same plugin; a new chat may be needed for discovery. An older separately registered `d-bond-agent` server is left unchanged. The setup window lists only paired, unpaused agents with an active direct listener for its optional **Send setup note** action. That action records delivery to the listener, not an AI turn or answer.

On an agent connection row, **Connect session…** shows the direct listener, Dot stream, and wake-check state separately for that exact connection ID. Registration checks the paired session once. The session acknowledges the wake message by its broker sequence. D-Bond keeps the receipt across restarts and shows live listener status separately. An explicit retry starts a fresh check. Codex registration belongs to the exact chat through Local Control `enroll_agent`. Its per-chat listener delivers the sender, original text and reply reference through that chat's discovered Desktop owner, steering the active turn or requesting a turn for an idle, open chat. Host acceptance and verified recipient readback are separate receipt stages; a content answer is a separate message. On supported Macs, the Claude `session-listen` repair command attaches a session-owned native inbox receiver. Other hosts may require a temporary Monitor whose deadline is controlled by Claude.

## App settings

Choose **Settings…** from the D-Bond menu or press **⌘,** while a D-Bond window is active. **Start D-Bond at login** controls automatic startup on this Mac. **Open Connections at launch** opens the Connections window each time; first-time setup opens it automatically. The menu and Settings show the installed version and build.

## Connect a service

1. Open **D-Bond → Open Connections… → Add Connection…**. Select **Herald notification**, **Generic HTTP producer**, or **Local demo echo**. Browser clients use an ordinary named service with their exact extension origin. Supply the service and topic that people and Dot should see.
2. Select the destination. For a browser extension that calls the broker directly, enter the exact extension ID from Chrome. Copy the endpoint and one-time secret into that service’s local settings. Existing connections have a **Setup** button with instructions. A pairing is saved even when the service is offline; in the window, “connected” means a receiver is listening.
3. Your service posts a stable event ID and a JSON payload to its loopback endpoint. For replies, it opens the authenticated event stream. Its integration decides what to do when a reply arrives. A public webhook uses an authenticated receiver in front of this loopback connection.

An existing authenticated webhook receiver can opt in at its dispatch boundary
without creating a second watcher. See the [optional source example](../examples/vollna/README.md).
For a neutral browser client, load the [browser transport demo](../adapters/browser-demo/README.md)
in a disposable Chrome profile and explicitly configure its local pairing.
Creating a connection does not modify any installed extension or external account.

## Connect Herald

In **Open Connections… → Add Connection…**, choose **Herald notification** and name it **Herald / Notifications**. When creating it, leave **Allow paired services to ask Dot to notify me** checked if you want this standing permission. If the connection already exists, reuse it and edit its **Rules → Plain notifications from paired services**. The internal rule ID is `general-notifications`. In Herald, open **Settings → General → D-Bond** to check the connection.

From a paired Codex or Claude session, ask “Have Dot send me a test notification through Herald.” The equivalent CLI command is:

```sh
dbond ask YOUR_SESSION_ALIAS "Send me a short test notification" --to "Herald / Notifications"
```

D-Bond identifies the sender and the Herald destination to Dot. Dot writes the title and body; D-Bond forwards that reply to Herald on the Mac. You do not have to repeat the request in Dot chat. The session’s reply tells you what Dot wrote. **Herald → History** confirms the notification reached Herald. For interactive questions with buttons, use Herald’s reply-capable connector.

## Attach your SOP once

In **Connections**, choose **Rules** next to a service. Select a rule in the list to edit its Markdown. **Add** starts a new rule; the arrows change the order Dot reads them. Use the toolbar for headings, bold text, lists, checklists, or code, and **Preview** to see the formatted instructions. **Save rules** (⌘S) applies all your edits together. If another editor saved first, your draft stays open: copy it before you reload the saved rules. The owner can change a connection’s rules without re-pairing, editing the webhook body, or changing another service. Each accepted service request carries the rule version it had at intake; retries of the same event retain it.

In Codex, install or enable [D-Bond Local Control](../adapters/codex/README.md) and open a local chat. Try: “Show the rules for Build monitor / Example. Add a rule to include the build result in its response. Preserve the other rules.” The plugin edits the same rules as the Mac window and refuses an edit based on an out-of-date version. The ordinary plugin details page does not have its own form.

Keep source data and revisions in the **payload**. The Mac attaches the
**owner SOP** separately; an incoming item cannot replace its own rules.
An application-specific action or notification requires that application's own
explicit integration and recorded result. D-Bond's transport receipts alone
do not prove that an external application changed its state.

## Inspect a delivery

Click **Events** next to the connection, or ask Local Control: “Trace the last event on Build monitor / Example. Show its SOP version and timestamps.” Trace times appear in this Mac’s timezone, with no fractional seconds; the ledger retains precise UTC timestamps for ordering. Older timestamps without fractional seconds remain readable. Use the same event ID across the watcher, D-Bond and your downstream service.

**Events** shows the ten most recent entries and updates as messages arrive. Each row shows a short preview, the other connection, direction, delivery state, and local time to the second. The preview takes up to two sentences or 210 characters. A fenced code block shows its language and first content line. A correlated Dot answer shows its own preview and the local time the broker observed the reply. **View complete log** opens the retained history in pages of 50, with event IDs, reply references, and recorded stages. Pages stay in place as new messages arrive. **Copy shown page** copies the visible page. Deleting a connection removes its retained history.

Direct agent messages appear in both participants' Events windows. Each entry says **Received from** or **Sent to** and names the other connection. A message sent to the same connection is labeled as such. It shows the broker sequence and, for a direct reply, **In reply to event** so a PONG can be tied to its PING. **Stored by D-Bond for recipient** means the broker persisted the message for replay; it does not prove that the recipient's AI session read it. An event ID can occur in multiple directions or from different senders. For a single trace, use the direction and exact counterpart ID shown by `dbond trace` or Local Control to select the intended row.

- A **queued** timestamp means the local Mac stored the event. **dot_stored** means the transport accepted it into storage.
- **dot_acknowledged** and **dot_replied** are separate. The displayed local times show when D-Bond observed those changes; they do not establish when a person saw a banner or a second app acted.
- An empty stage is unknown, not zero elapsed time. Older events may have no timestamp history. Reconcile an uncertain send using the original event ID.


## After an interruption

The service keeps the same endpoint, credential and event ID across restarts. Reopen the receipt stream with the last sequence your service durably handled. If a POST response is lost, resend the same ID and unchanged payload; creating a new ID starts a new message. The broker retries temporary transport failures with backoff. It checks the native request store before sending again. A rejected or unsafe-to-replay message has an explicit receipt. A paused or deleted link does not silently become active.

The native Dot subscription and its account grant are separate from your service pairing. The host can renew them automatically; an owner revocation, a disabled grant, or a missed host renewal can still prevent a wake. While Dot is offline, its stored messages are subject to the native host's expiry. The broker can restart the Mac app and its observer; it cannot wake a powered-off computer or guarantee Dot's response time.

## Service icons and settings

The menu shows how many connections exist and how many have an open listener. Open **Connections** to manage them.

| Section | What it contains | Removing or stopping a connection |
| --- | --- | --- |
| Dot | The shared Dot connector and any separately paired Dot sessions | **Dot settings…** manages the shared route. Stopping the active built-in connector interrupts every workflow that uses it. Deleting a Dot session pairing requires updating workflows that address that pairing. |
| Workflow services | Webhook watchers, browser extensions, notifications and other tools | Pause to keep the endpoint and rules. After deletion, create a replacement pairing, restore its rules and update dependent tools with the new address or credential. |
| Agent sessions | Individual registered Codex and Claude chats | After deletion, register the chat with a fresh alias or register a new chat. Each new pairing has a new address. |

Deleting a pairing also removes its stored messages and receipts. A service that sends events to Dot appears under **Workflow services**. The shared Dot connector has its own settings in **Dot**.

**Connectors…** checks which client adapters are already installed; **Add Connection** pairs an HTTP service. Each row keeps its setup, rules, activity, pause and delete controls together. Setup shows the address and lets you choose a custom logo. The app resolves Claude and Codex logos from the locally installed apps. Other owner-named services use a neutral system symbol until you choose custom artwork for that connection; no vendor service artwork is bundled.

[Direct agent messages](../adapters/direct/README.md) documents the CLI commands, stdio tools, streaming inbox, and which setup steps need an open client session.

In **Connectors…**, use **Check again** after changing a host setting. The Codex row shows Local Control and whether direct messaging is included; an older standalone direct server appears as a legacy installation. Claude Code and Claude Desktop remain separate installations. A disabled or conflicting configuration is shown for review. From any updated D-Bond MCP adapter, call `local_status` to check whether the Mac app is installed and its local bridge is reachable; this does not register a service or establish that an agent is listening.

For a paired Claude Code chat on macOS, including Code opened in Claude Desktop, register the exact session to connect its native inbox. Its D-Bond receiver can listen throughout the open session. Claude’s ordinary Monitor tool has a time limit and serves as a fallback on hosts without a native route.
