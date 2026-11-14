# D-Bond

D-Bond is a Mac menu bar app that connects Dot, agent chats and local services. Pair a service once, give it a name, and send messages in either direction. The Mac stores connections, rules and delivery history across restarts.

**Version 0.4.1** · [Download for Apple silicon](https://github.com/ivg-design/d-bond/releases/latest) · [Changelog](CHANGELOG.md)

## Install

Download **D-Bond-macOS-arm64.zip**, extract it, and move **D-Bond.app** to Applications. Open the app and choose **Connectors…** in the menu bar. The Apple silicon binary includes the Dot host, Python and the connector installers. It requires macOS 13 or later. The release page lists the signature and notarization status for that download.

| Connect | From the app |
| --- | --- |
| Dot | Open **Connectors → Configure Dot Connector…**. Set up HTTPS access to your Mac, register your MCP app in ChatGPT, and connect your Dot once. |
| Herald | Open Herald in **Connectors**. Choose **Install Herald…** to download and open its verified disk image. Drag Herald into Applications, then connect notifications. |
| Claude | Choose the Desktop extension or Claude Code plugin. Approve the installation in Claude and register the desired chat. |
| Codex | Install the packaged local tools and register the desired chat. |
| Another service | Use **Add Connection…** for a named webhook sender, CLI client or browser extension. The app provides the local endpoint and the pairing credential. |
| Vollna | Use the optional example in **Connectors** with your own account and webhook receiver. The example includes a local preview. [Vollna example](examples/vollna/README.md) |

Open **Connections** to see **Dot**, **Workflow services** and **Agent sessions**. Each removal prompt explains how to recover that connection. Use **Pause** when you want to keep its address, rules and history. Open its rules editor to write the Markdown SOP that accompanies each incoming event. **Events** records request previews, receipts and replies using local display times. **Start D-Bond at login** is available in Settings.

## Connect your existing Dot

1. Open Dot setup and choose **Set Up HTTPS Access…**. Use your own stable HTTPS address and reverse proxy, or configure a named Cloudflare tunnel in the app. Route the public hostname to the restricted local ingress at `127.0.0.1:18770`. D-Bond’s service-pairing and owner APIs stay on loopback.
2. In **Open ChatGPT Plugins**, register your own MCP app at **https://your-host.example/mcp**. Copy the OAuth callback shown by ChatGPT into D-Bond. Configure the host and use **Show OAuth Setup** for its registration and one-time owner consent.
3. Connect that MCP app to your Dot. Ask Dot once to subscribe to `app.message.created` on channel `d-bond`, acknowledge incoming requests and return replies through the connector. Use **Check Status**, then **Use Built-in** when D-Bond shows one active subscription. The optional private Dot plugin carries more detailed handling instructions. [Dot plugin](adapters/dot/README.md)
4. Send a question from a paired service. The event history shows when the Mac stored it, when Dot acknowledged it, and when Dot replied. Each service shares the same standing Dot connection.

OpenAI Secure MCP Tunnel is being evaluated separately for private workspaces. The public setup wizard uses the verified HTTPS route described above. Vollna and other third-party webhook senders still need a reachable receiving URL of their own. [How Secure MCP Tunnel works](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

D-Bond stores the subscription and its signing material locally. An existing installation can keep its older connection while configuring the built-in host. Dot owns its callback registration and expiration. A revoked grant or expired subscription can require action in Dot. The Mac and its HTTPS route must be reachable for cloud-to-Mac calls. [MCP Events](https://developers.openai.com/plugins/build/mcp-events)

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

The result is `dist/D-Bond.app`. To install into `~/Applications` with a CLI shortcut and per-user crash supervision, quit a running D-Bond and run `bash scripts/install_app.sh`. To reproduce a tagged release, run `bash scripts/package_release.sh v0.4.1`. Its app archive, Claude Desktop extension and SHA-256 checksums go into `dist/releases/v0.4.1/`.

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
