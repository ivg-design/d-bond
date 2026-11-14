# Changelog

Versions below refer to the macOS app; connector packages have their own version numbers.
Historical tags preserve the public source for each app version. Earlier releases provide source archives.

## 0.5.0 — 2026-11-14 · build 16

- Connect a private ChatGPT workspace to the Mac through OpenAI Secure MCP Tunnel. Dot can wake, acknowledge requests and return replies without a public hostname or a Cloudflare account.
- Reuse the saved tunnel configuration after restarting D-Bond. The wizard shows the tunnel connection and Dot’s subscription separately.

## 0.4.1 — 2026-11-14 · build 15

- Separate agent chats, the shared Dot connection and workflow services in Connections. Removal and stop prompts explain what will stop working and how to restore it.
- Include a credential-free, importable n8n workflow and a step-by-step Vollna guide for a new Mac or an existing webhook receiver.
- Add an opt-in example for direct Claude–Codex messages between two local Backplane chats.
- Fix the bundled CLI when launched from a symlink on the user’s PATH.

## 0.4.0 — 2026-11-14 · build 14

- Set up the Dot MCP Events host from the Mac app. The app includes the host, Python runtime, durable message store and connector installers.
- Pair an existing Dot once with a stable HTTPS endpoint. Inspect its subscription and choose the active route in Settings. Existing installations can keep their previous connection during setup.
- Add a Connectors window for Dot, Herald, Claude, Codex and generic services. It includes a browser messaging example and an optional Vollna sender using your own webhook receiver.
- Distribute the generic public app and sanitized historical source archives. Keep service-specific configurations and artwork on the owner’s Mac.

## 0.3.10 — 2026-11-14 · build 13

- Add local Codex delivery diagnostics: when a message was first observed, when its
  Desktop owner became unavailable or returned, and which stream/message remains
  pending. Times include whole seconds and the local UTC offset.
- Keep listener readiness degraded while delivery or its receipt is pending, and
  avoid rewriting unchanged health snapshots.
- Make `ivg-design/d-bond` the canonical repository, add the MIT license and upstream
  attribution, and update installation and release documentation.
- Publish tagged source history and the first downloadable macOS Apple
  Silicon app archive with a checksum and a separate Claude Desktop MCPB package.

A durable Codex chat currently needs a loaded Desktop owner for direct delivery.

## 0.3.9 — 2026-11-13 · build 12

- Report Claude listener startup and live receiver health accurately, including
  startup delays and unavailable listeners.

## 0.3.8 — 2026-11-13 · build 11

- Keep Claude Code listeners attached for the lifetime of their host session.
- Preserve inbox position across reconnects and connect enrollment to the installed
  listener instead of relying on a temporary Monitor alone.

## 0.3.7 — 2026-11-13 · build 10

- Route Codex messages to the local or durable Desktop host that owns the paired
  chat, retain that host binding, and recheck its owner before delivery.
- Distinguish an uncertain host result from a confirmed delivery receipt.

## 0.3.6 — 2026-11-13 · build 9

- Deliver the sender's original message and reply reference into the exact paired
  Codex chat: steer an active turn or request a turn for an idle, open chat.
- Acknowledge host acceptance silently and retain uncertain submissions without
  automatically sending them again.
- Show delivery receipts separately from a recipient's reply.

## 0.3.5 — 2026-11-13 · build 8

- Deliver paired Herald requests using the receiving connection's saved rules.

## 0.3.4 — 2026-11-13 · build 7

- Add Settings, intake review history, startup controls and local Herald setup.
- Read intake admissions from the running broker and keep startup guidance together.

## 0.3.3 — 2026-11-13 · build 6

- Reuse saved session wake-check receipts across reconnects and retain checks over
  a broker restart.
- Use Dot's Herald sender for requests routed through Dot.

## 0.3.2 — 2026-11-12–13 · build 5

- Add paired-session registration, wake checks and a per-chat Codex listener.
- Show compact two-way event history with local timestamps, message previews and
  access to the complete log.
- Improve Vollna outcome previews, bounded connection reads, proposal settlement
  labels and generic rule names.

## 0.3.1 — 2026-11-12 · build 4

- Detect installed agent tools and improve connector setup, rule editing and event
  logs.
- Prevent packaged Python adapters from writing caches into signed app resources.

## 0.3.0 — 2026-11-12 · build 3

- Add direct messaging between paired agents, connector installers and timestamped
  delivery receipts.
- Add an agent setup window and a direct MCP adapter.

## 0.2.0 — 2026-11-12 · build 2

- Introduce the macOS menu bar broker, local pairing, connection rules, persisted
  requests, event traces and local Codex/Claude plugins.
- Add Chrome-client origin pairing, local receipt times and webhook job titles.
- Recover interrupted Dot delivery without re-pairing and add the D-Bond artwork.
