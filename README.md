# D-Bond

D-Bond is a Mac menu bar app that connects Dot, agent chats and local services. Pair a service once, give it a name, and send messages in either direction. The Mac stores connections, rules and delivery history across restarts.

**Version 0.5.1** · [Download for Apple silicon](https://github.com/ivg-design/d-bond/releases/latest) · [Changelog](CHANGELOG.md)

## Install

Download **D-Bond-macOS-arm64.zip**, extract it, and move **D-Bond.app** to Applications. Open the app and choose **Connectors…** in the menu bar. The Apple silicon binary includes the Dot host, Python and the connector installers. It requires macOS 13 or later. The release page lists the signature and notarization status for that download.

| Connect | From the app |
| --- | --- |
| Dot | Open **Connectors → Configure Dot Connector…** and choose **OpenAI private tunnel…** or **Set up public HTTPS…**. Register the connector in ChatGPT and connect your existing Dot once. |
| Herald | Open Herald in **Connectors**. Choose **Install Herald…** to download and open its verified disk image. Drag Herald into Applications, then connect notifications. |
| Claude | Choose the Desktop extension or Claude Code plugin. Approve the installation in Claude and register the desired chat. |
| Codex | Install the packaged local tools and register the desired chat. |
| Another service | Use **Add Connection…** for a named webhook sender, CLI client or browser extension. The app provides the local endpoint and the pairing credential. |
| Vollna | Use the optional example in **Connectors** with your own account and webhook receiver. The example includes a local preview. [Vollna example](examples/vollna/README.md) |

Open **Connections** to see **Dot**, **Workflow services** and **Agent sessions**. Each removal prompt explains how to recover that connection. Use **Pause** when you want to keep its address, rules and history. Open its rules editor to write the Markdown SOP that accompanies each incoming event. **Events** records request previews, receipts and replies using local display times. **Start D-Bond at login** is available in Settings.

## Connect your existing Dot

Starting with only the ChatGPT app on an Apple silicon Mac, you’ll create a Dot, connect that Dot to D-Bond once, and then pair each service with the Mac. This route uses outbound HTTPS and works without a domain, router changes or Cloudflare.

1. **Create your Dot in ChatGPT.** Open **Dots** in the workspace you want to use, follow the introduction and give your Dot a name. You can skip the suggested plugins. If Dots is missing, check eligibility first. OpenAI currently lists eligible Pro and Business Premium accounts and eligible Enterprise, Edu and Healthcare workspaces, with regional and workspace controls. This connection uses the private plugin and does not require giving Dot general access to the Mac. [Create a Dot](https://learn.chatgpt.com/docs/dots/getting-started#create-your-dot) · [Availability](https://learn.chatgpt.com/docs/dots#access)
2. **Install D-Bond.** Download the Apple silicon ZIP, unzip it and move **D-Bond.app** into **Applications** on macOS 13 or later. On first launch, click **Set Up Dot → OpenAI private tunnel…**. You can reopen it from the menu bar under **Settings…**. If the wizard offers **Get Homebrew…**, complete Homebrew setup, return and click **Check connection**. Choose **Install OpenAI client…** if offered. The wizard detects existing installations. [Download D-Bond](https://github.com/ivg-design/d-bond/releases/latest)
3. **Connect your Mac to OpenAI.** In the wizard, choose **OpenAI tunnels…**. In your OpenAI Platform organization, create a tunnel and associate it with that organization **and the ChatGPT workspace containing your Dot**. Copy the tunnel ID. Under **API keys…**, create a **Restricted** runtime key with **Tunnels → Read and Use**. Put the ID and key into D-Bond and click **Save & Start**. The key goes into the Mac’s login Keychain. Creating the tunnel requires organization-level **Read + Manage**. Running or selecting it requires **Read + Use**. For a managed organization, its owner or role administrator can grant those permissions. [OpenAI tunnel setup and permissions](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
4. **Install the private plugin in ChatGPT.** Choose **Open ChatGPT Plugins…** in the wizard. In the browser, switch to the same workspace, then choose **+ → Add custom MCP server**. Name it **D-Bond**, choose **Tunnel**, select your tunnel, and choose **No authentication**. Review the warning, choose **Create as a plugin**, then install and enable it for the account your Dot uses. Keep its audience private to you. Anyone with access can read requests or send messages as the D-Bond owner. Managed workspaces must allow custom MCP plugins and event-triggered tasks. [Custom MCP setup](https://developers.openai.com/api/docs/guides/custom-mcp-server) · [MCP Events availability](https://developers.openai.com/plugins/build/mcp-events)
5. **Subscribe once.** Choose **Copy Dot setup message** in D-Bond and send it in your Dot’s conversation. It asks Dot to listen immediately for `app.message.created` on channel `d-bond`, acknowledge each request, and reply through the plugin. When Dot confirms, choose **Check connection**. Look for **Dot’s standing subscription is verified**, then click **Use for Dot workflows**. Future service pairings share this subscription.
6. **Try a round trip.** With D-Bond open, run the following in Terminal. This pairs **Terminal / First connection** and sends a message from it. You can see the connection, ACK and reply in D-Bond’s **Connections** and **Events**.

   ```sh
   dbond="/Applications/D-Bond.app/Contents/Resources/dbond"
   "$dbond" enroll first-check --service Terminal --topic "First connection" --destination dot
   "$dbond" ask first-check "Reply through D-Bond with exactly: First connection received."
   ```

7. **Keep it running.** Enable **Settings → Start D-Bond at login** and keep the Mac awake and online when you need immediate delivery. To check persistence, quit and reopen D-Bond, then run just the `ask` command again. Use the same tunnel, plugin, subscription and service pairing. ChatGPT refreshes expiring event subscriptions. If the runtime key expires or is revoked, open the wizard, stop the tunnel if running, keep the same ID, enter the replacement key and choose **Save & Start**. [MCP Events subscription lifecycle](https://developers.openai.com/plugins/build/mcp-events)

For the **public HTTPS** option, forward your hostname to the restricted ingress at `127.0.0.1:18770` using your reverse proxy or a named Cloudflare tunnel. Register the public `/mcp` URL in ChatGPT, copy the OAuth callback into D-Bond, then complete owner consent in **Show OAuth Setup**. Ask your Dot to subscribe to `app.message.created` on the `d-bond` channel through that HTTPS plugin. The optional private Dot plugin supplies detailed message-handling instructions. [Dot plugin](adapters/dot/README.md)

A live two-message test with an existing Dot measured 20.9 seconds to its first ACK and 14.9 seconds after restarting the Mac tunnel client, Dot host and D-Bond broker. The sending service received both exact replies using the original subscription. [Reproduce the restart test](examples/openai-tunnel/README.md)

Vollna and other webhook senders use a separate receiving address. Their local receiver forwards each event through the same D-Bond connection to Dot. [Vollna intake](examples/vollna/README.md)

## Agent chats

In the desired Codex or Claude chat, say **“Register this session in D-Bond.”** After registration, ask it to message another connection by name. Direct agent-to-agent messages travel through the Mac independently of Dot. Each host determines whether an incoming message can start a turn. A durable Codex cloud chat can remain pending until its desktop owner loads. [Codex listener](adapters/codex-session/README.md)

For two chats running inside the local Backplane UI, see the [Backplane direct relay example](examples/backplane/README.md). It targets the exact Backplane thread and provider.

The bundled CLI and MCP adapters support service enrollment, messaging and receipts. [CLI guide](adapters/cli/README.md) · [Codex](adapters/codex/README.md) · [Claude](adapters/claude/README.md) · [Generic clients](adapters/direct/README.md) · [Browser example](adapters/browser-demo/README.md)

## Build from source

The current packaged runtime targets Apple silicon. To build locally, install Xcode Command Line Tools with Swift 5.9 or later and a build-time Python 3.11 or later.

```sh
git clone https://github.com/ivg-design/d-bond.git
cd d-bond
bash scripts/bundle.sh
```

The result is `dist/D-Bond.app`. To install into `~/Applications` with a CLI shortcut and per-user crash supervision, quit a running D-Bond and run `bash scripts/install_app.sh`. To reproduce a tagged release, run `bash scripts/package_release.sh v0.5.1`. Its app archive, Claude Desktop extension and SHA-256 checksums go into `dist/releases/v0.5.1/`.

```sh
swift test
swift build -c release --product dbond-probe
python3 -B -m unittest discover -s Tests
python3 -B -m unittest discover -s adapters/cli
python3 -B -m unittest discover -s adapters/codex-session
node adapters/browser-demo/test.mjs
```

## License

D-Bond is MIT licensed, copyright 2026 IVG Design. The embedded host, runtime and other dependencies retain their own notices. [MIT license](LICENSE) · [Third-party notices](THIRD_PARTY_NOTICES.md)
