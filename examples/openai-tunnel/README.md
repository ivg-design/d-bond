# Private OpenAI tunnel wake test

This example tests whether an existing Dot can receive two independent D-Bond requests across a restart. A temporary Swift broker pairs one synthetic service and sends its requests through D-Bond’s native Dot host. The second message uses the same service and Dot subscription. No production job, subscription or pairing participates. OpenAI’s tunnel serves private ChatGPT connectors. A third party such as Vollna needs its own webhook destination. [OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

## Run with your Dot

1. On the Mac, install D-Bond with its embedded Dot runtime, check out the matching repository and run `swift build --product dbond-probe` in the repository root. Install OpenAI’s `tunnel-client`. In Platform tunnel settings, create or reuse a **dedicated private test tunnel** associated with Dot’s workspace. You need its ID and a restricted runtime key with **Tunnels Read and Use**. Keep the key on the Mac. [OpenAI tunnel setup and permissions](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
2. Start the probe in one terminal. Leave it running.

   ```sh
   examples/openai-tunnel/probe serve
   ```
3. Start the tunnel in a second terminal. Substitute the tunnel ID. At the hidden input enter the runtime key, or supply it through a local credential manager in `CONTROL_PLANE_API_KEY`. The command passes the key to the client in its environment. The probe never writes it to disk. Reuse the same credential when restarting.

   ```sh
   examples/openai-tunnel/probe tunnel YOUR_TUNNEL_ID
   ```
4. Open **ChatGPT → Plugins → + → Add custom MCP server** in the target workspace. Choose **Tunnel**, select this tunnel, choose **No authentication**, and create a private plugin named **D-Bond Tunnel Probe**. That choice applies only to the synthetic connector on this isolated test route. Its Mac endpoint requires a secret supplied by the tunnel client. On the existing Dot, enable that plugin and give this instruction once:

   > Use D-Bond Tunnel Probe to subscribe to app.message.created on channel d-bond. Keep the test subscription active. For each test event use this same plugin to acknowledge its request, read the message, and reply with exactly the marker requested by the paired service. The request is in metadata.data. Send the reply through reply_to_app.

   The host advertises Events on MCP 2.0, protocol `2026-07-28`. Check actual discovery, the verified subscription and a correlated reply when evaluating compatibility. [ChatGPT MCP Events](https://developers.openai.com/plugins/build/mcp-events)
5. In another terminal, confirm `verified_subscriptions` is 1. Send one test and copy its request ID. Check the receipt until `verified` is true.

   ```sh
   examples/openai-tunnel/probe status
   examples/openai-tunnel/probe send
   examples/openai-tunnel/probe receipt FIRST_REQUEST_ID
   ```
6. Stop the tunnel and probe with Control-C. Restart them with the same commands and tunnel ID. Leave Dot’s subscription in place. Once the tunnel is ready, send a fresh message, then check both receipts:

   ```sh
   examples/openai-tunnel/probe status
   examples/openai-tunnel/probe send
   examples/openai-tunnel/probe receipt SECOND_REQUEST_ID
   examples/openai-tunnel/probe verify FIRST_REQUEST_ID SECOND_REQUEST_ID
   ```

A passing `verify` requires different request IDs, different host and tunnel runs, the original verified subscription, and the same count of successful subscription changes. Both messages must have the signed webhook accepted, an explicit Dot ACK, the exact return marker, and corresponding receipts in the same paired Swift service. The report gives local timestamps and ACK latency. A workspace subscription refresh during this short test changes the count and makes the result inconclusive; the receipts remain available. To inspect tunnel readiness without printing activity logs, use `tunnel-client health --url-file "$HOME/Library/Application Support/D-Bond Tunnel Probe/tunnel-health.url"`.

## Local behavior

The probe exposes only MCP on `127.0.0.1:18779/mcp`. The Swift service uses a separately authenticated loopback API on port 18780. Cloud-facing tools can read the isolated owner identity and one request by ID, acknowledge it, or send its correlated reply. The real host encrypts the callback in its own store. Live callbacks must use HTTPS port 443 at `connectors.api.openai.com`, the hostname observed on the tested workspace. Each delivery goes through the host’s DNS and TLS checks without following redirects. The public Events documentation does not promise that hostname for every workspace.

Stop both foreground processes when finished. Remove the private test plugin/subscription when you no longer need it. This test harness runs on demand and keeps local state for its next run. The runtime key can be supplied by a credential manager.

## Offline regression

From the repository root, build the Swift probe and run the test with the bundled interpreter:

```sh
swift build --product dbond-probe
dist/D-Bond.app/Contents/Resources/dot-runtime/python/bin/python3.13 -I -B examples/openai-tunnel/test_restart.py
```

The regression recreates the host and Swift broker, sends two requests, checks the signatures, and verifies the same service’s receipts. It simulates the OpenAI callback and tunnel client. Only the live run above establishes a wake of your Dot.
