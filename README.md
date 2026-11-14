# D-Bond

D-Bond is a Mac menu bar app that connects Dot, agent chats and local services. Pair a service once, give it a name, and send messages in either direction. The Mac stores connections, rules and delivery history across restarts.

**Version 0.5.0** · [Download for Apple silicon](https://github.com/ivg-design/d-bond/releases/latest) · [Changelog](CHANGELOG.md)

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

In **Settings → OpenAI private tunnel…**, set up OpenAI Secure MCP Tunnel for your Dot’s workspace. The Mac opens an outbound connection to OpenAI. You can use it without a domain, router changes or Cloudflare.

1. Create a dedicated tunnel in OpenAI Platform and select the ChatGPT workspace that contains your Dot. Create a restricted runtime key with **Tunnels → Read and Use**. If prompted, use **Install OpenAI client…** in the wizard. Enter the tunnel ID and key, then choose **Save & Start**. D-Bond keeps the runtime key in your login Keychain. [OpenAI tunnel setup](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
2. In **ChatGPT → Plugins → Add → Add custom MCP server**, give the connector a memorable name, choose **Tunnel**, select the same tunnel and choose **No authentication**. Connect the plugin. Keep this connector private to its owner. Anyone who can use it can act as that D-Bond owner, including reading requests and sending messages to paired services.
3. Choose **Copy Dot setup message** and send it once in your existing Dot chat. The example below assumes you named the plugin D-Bond. Return to the wizard, choose **Check connection**, then **Use for Dot workflows** when the subscription is verified. One subscription serves all paired services.

   > Use my private D-Bond plugin to subscribe to `app.message.created` on channel `d-bond`. Keep the subscription active. When a message arrives, acknowledge the request, read its instructions, and return the answer to the sending service through the same plugin.

4. Send a question from one paired service. In **Events**, check for Dot’s ACK and the answer received by the service. Enable **Start D-Bond at login** to restore the local connection when you sign in to your Mac. Revoked or expired credentials need renewal. [MCP Events](https://developers.openai.com/plugins/build/mcp-events)

For the **public HTTPS** option, forward your hostname to the restricted ingress at `127.0.0.1:18770` using your reverse proxy or a named Cloudflare tunnel. Register the public `/mcp` URL in ChatGPT, copy the OAuth callback into D-Bond, then complete owner consent in **Show OAuth Setup**. Subscribe your existing Dot as in step 3. The optional private Dot plugin supplies detailed message-handling instructions. [Dot plugin](adapters/dot/README.md)

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

The result is `dist/D-Bond.app`. To install into `~/Applications` with a CLI shortcut and per-user crash supervision, quit a running D-Bond and run `bash scripts/install_app.sh`. To reproduce a tagged release, run `bash scripts/package_release.sh v0.5.0`. Its app archive, Claude Desktop extension and SHA-256 checksums go into `dist/releases/v0.5.0/`.

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
