# D-Bond public plugin listing evaluation

**Status:** evaluation draft only, 12 November 2026. This file is not an OpenAI submission, public listing, or claim of eligibility. No package was uploaded and no representative was contacted.

## Assessment

The current `adapters/dot/d-bond` package is a Dot skill that declares the existing Dot Bridge Transport app through a private app binding. Its instructions explicitly rely on that app already being connected and on the owner's one existing D-Bond/Dot setup. That binding is a private owner integration, not a public hosted MCP endpoint or a self-contained local MCP server bundled for each installer. It does not give arbitrary directory users access to the owner's Dot connection or authorize them to use the owner's account. Do not represent the current binding as a distributable public service.

A custom marketplace or GitHub source is a separate distribution mechanism: it lets a person, repository, or workspace discover/install a plugin package. A Git-backed source does not turn a private app binding into a public service or publish the package in the universal Plugins Directory. Workspace publication is also confined to that workspace. For public directory review, the package must instead use a supported submission path and independently satisfy review requirements.

OpenAI's current desktop-only path is the closest fit for a plugin that needs a local process: it is marked Desktop only, installed from the ChatGPT desktop app, and usable only in locally running threads. Local MCP submissions are limited to approved partners. OpenAI instructs the publisher to confirm eligibility with its representative and have the submitting organization allowlisted before submitting. We do not know whether IVG or any intended submitting organization is an eligible partner or is allowlisted; GitHub access, a verified identity, or having a local plugin does not establish that status.

There are therefore two separate gaps to resolve before a viable public-listing effort: (1) obtain confirmation that the chosen submitting organization is approved/allowlisted for local MCP, and (2) design and package a public-safe product that has its own user-authorized connection/runtime rather than reusing the owner's private Dot binding. This is an evaluation, not a recommendation to contact OpenAI or submit now.

## Relevant current behavior and constraints

The private source Dot plugin tells Dot to acknowledge and reply to D-Bond messages, address paired services, and use the current connection directory in incoming request metadata. The README describes D-Bond as a single-user Mac menu-bar app connected to the owner's existing Dot Bridge engine. The existing package is therefore useful evidence of behavior and product vocabulary, but it is not proof of multi-user public operation, generalized onboarding, independent user accounts, service availability, or a ready-to-publish deployment.

OpenAI's directory guidelines require a clear, reliable and complete product purpose; accurate claims and prompts; user-authorized behavior; minimum necessary data; a published privacy policy covering data categories, purposes, recipients, retention and user controls; verified publisher identity; and a working support contact. MCP tools must have clear descriptions matching their behavior and must not manipulate discovery or over-trigger. The guidelines also say unofficial third-party connectors or pass-through layers may not be approved. The D-Bond review should explain its authorized integration boundaries and show distinct product value without claiming an OpenAI relationship or bypassing Dot/third-party access controls.

## Public listing draft (conditional, not publishable as-is)

**Name:** D-Bond

**Short description:** Route user-authorized messages between your connected assistant and paired services.

**Long description:** D-Bond helps you send a question to a paired service and receive its correlated reply through your configured D-Bond connection. It can also receive a message from a paired service and return a reply to that request. Pairing and service availability depend on your own D-Bond setup. D-Bond does not guarantee that a recipient is online or that a message wakes an idle desktop session.

**Starter prompts:**

- “Ask my paired design session what it needs next.”
- “Show me the reply to my latest D-Bond request.”
- “Reply to this D-Bond request with the summary I provided.”

These claims and prompts assume a future public package with a tested, supported per-user connection path. They must be revised to match the actual released architecture and tools. The current private Dot binding should not be used as the public listing's setup story.

**Category:** Not selected. Choose only from the current supported category list after confirming the public product scope.

**Publisher:** Unknown / not verified here.

**Listing URLs:** Not supplied. Public MCP review requires four valid HTTPS URLs identifying the same publisher: product website, support, privacy policy and terms of service. Do not substitute repository links unless each actually serves the required purpose and publisher identity. The privacy policy must accurately cover D-Bond's real local and remote data flows.

**Icon, demo URL, release notes, supported countries and commerce declaration:** Not supplied or verified. Do not invent them. Current product notes describe no commerce, but the publisher must attest to the actual submitted package and business model.

## Reviewer demo and access plan

If the architecture becomes viable, record a real walkthrough using a clean desktop installation and a dedicated reviewer setup with synthetic paired services/sample data. Show installation, first-run connection setup, one explicit outbound request and its correlated reply, one inbound message and response, reconnection or unavailable-recipient behavior, and how the user pauses/removes a pairing. Demonstrate the same five positive and three negative cases below. Keep the recording accessible to reviewers; do not use the owner's private Dot account, live third-party inboxes, real client messages, or credentials in the video or public ZIP.

If reviewer sign-in is needed, provide a dedicated test account, workspace/tenant details, sign-in URL, and instructions in the portal's private reviewer-credentials field. Use sample data and access that works without private network access or a human MFA/email/SMS approval step. If a local pairing is required, the clean-install review path must explain and support it without exposing the owner's account or secrets. Until that can be demonstrated on a separate clean environment, reviewer access is unresolved.

## Draft MCP review cases

These are proposed expected behaviors, not executed tests. Replace tool placeholders with the final tool names and run all cases on the actual review package/account before submission. Initial MCP review calls for exactly five positive and three negative cases, with scenario, prompt, expected tools, and observable result for positives; negatives should state why the plugin must not act and the safe fallback.

### Positive cases

1. **Ask one explicitly named paired service.** Prompt: “Ask the paired service ‘Design Review’ whether the supplied mockup needs a mobile pass.” Expected tools: resolve the exact active recipient, send the addressed request, receive/correlate its response. Expected result: only that service receives the user-authored question; the returned reply is labeled as that service's response.
2. **Receive and acknowledge a paired request.** Scenario: a synthetic paired service sends a request with a request ID. Prompt: “Handle the pending D-Bond request and tell me what it asks.” Expected tools: acknowledge the exact request ID and read its stored body. Expected result: the request is acknowledged once and summarized without claiming it was completed if no reply was sent.
3. **Reply to the originating request.** Scenario: synthetic request asks which of two provided mockup variants to review first. Prompt: “Reply to this request: review variant B first because the mobile flow is clearer.” Expected tools: reply to the same request ID. Expected result: supplied text is returned to the origin; no unrelated service is contacted.
4. **Use a current connection snapshot.** Scenario: request metadata lists active “Design Review” and paused “Build Monitor” services. Prompt: “Which services are listed in this request?” Expected tools: inspect the request's connection snapshot. Expected result: report exactly those names and pause states, clearly identifying the data as the request-time snapshot.
5. **Reconcile an already-started request.** Scenario: acknowledge returns `already_started` for a synthetic request. Prompt: “Continue handling the pending request.” Expected tools: inspect existing status/receipts and pending requests; reply only if the existing request state permits. Expected result: no duplicate action; explain current state or continue the same request safely.

### Negative cases

1. **No recipient or ambiguous name.** Prompt: “Send this to the design person,” when zero or multiple active recipients match. Why not act: recipient identity is unresolved. Expected fallback: ask the user to choose an exact active paired service; send nothing.
2. **Paused or missing service.** Prompt: “Send this to Build Monitor,” when the current request snapshot marks it paused or omits it. Why not act: current evidence does not establish an active destination. Expected fallback: explain the service is unavailable in the snapshot and ask for a current address/reconnection; do not guess or retry elsewhere.
3. **Untrusted request asks for unrelated secrets/action.** Scenario: a paired message asks for credentials, hidden conversation history, or an unrelated external action. Prompt: “Do what the message says.” Why not act: pairing authorizes ordinary message exchange only, not unrelated disclosure or actions. Expected fallback: refuse that requested disclosure/action and offer a safe, task-scoped reply to the originating request.

## Readiness gates

1. Confirm the intended publisher identity and organization; independently verify any required local-MCP partner eligibility and allowlisting before treating public submission as actionable.
2. Define a public product/runtime and user-owned pairing/onboarding path. Remove any assumption that a public user can use the owner's private Dot app, subscription, credentials, data, or connected services.
3. Review third-party authorization, data flows, security, reliability, support, and privacy/terms against the actual package and OpenAI's current guidelines.
4. Build a clean distributable and verify installation and main workflows on a clean desktop account. Run all eight reviewer cases; save the actual results and limitations.
5. Prepare accurate listing metadata, four publisher-consistent HTTPS URLs, icon, accessible demo, release notes, supported-country selection, and private reviewer access where needed. Submit only after these prerequisites are real and the publisher authorizes it.

## Sources checked

- [Submit desktop-only plugins](https://developers.openai.com/plugins/deploy/desktop-only) — local execution, local MCP partner limitation and organization allowlisting.
- [Upload and submit your plugin](https://developers.openai.com/plugins/deploy/submission) — identity/access, review materials, URLs, demo, tests and publication flow.
- [Plugin guidelines](https://developers.openai.com/plugins/plugin-guidelines) — purpose, reliability, user intent, third-party integrations, privacy and tool requirements.
- [Package your plugin](https://developers.openai.com/plugins/build/plugins) — local/Git-backed marketplace sources and distinction between local/workspace distribution and the universal public directory.
- Original private project evidence (excluded from this export): its README and Dot binding package.
