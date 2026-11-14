import AppKit
import DBondCore

/// A private ChatGPT plugin, the Mac tunnel client and Dot's callback are three independent stages.
@MainActor
final class OpenAITunnelSetupWindow: NSWindowController, NSWindowDelegate {
    private let connector: DotConnector
    private let runtime: OpenAITunnelRuntime
    private let onRouteChanged: () -> Void
    private let idField = NSTextField(string: "")
    private let keyField = NSSecureTextField(string: "")
    private let clientStatus = NSTextField(wrappingLabelWithString: "")
    private let tunnelStatus = NSTextField(wrappingLabelWithString: "")
    private let subscription = NSTextField(wrappingLabelWithString: "")
    private let workflowRoute = NSTextField(wrappingLabelWithString: "")
    private let start = NSButton(title: "Save & Start", target: nil, action: nil)
    private let stop = NSButton(title: "Stop tunnel", target: nil, action: nil)
    private let install = NSButton(title: "Install OpenAI client…", target: nil, action: nil)
    private let check = NSButton(title: "Check connection", target: nil, action: nil)
    private let subscribe = NSButton(title: "Copy Dot setup message", target: nil, action: nil)
    private let useRoute = NSButton(title: "Use for Dot workflows", target: nil, action: nil)
    private var snapshot: DotConnectorStatus?
    private var busy = false
    private var checking = false
    private var feedback = ""
    private var copiedSetup = false
    private var observer: NSObjectProtocol?

    init(connector: DotConnector, runtime: OpenAITunnelRuntime, onRouteChanged: @escaping () -> Void) {
        self.connector = connector
        self.runtime = runtime
        self.onRouteChanged = onRouteChanged
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 625, height: 646),
                              styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        super.init(window: window)
        window.title = "Private Dot connection"
        window.identifier = .init("dbond.openaiTunnel.window")
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: 540, height: 410)
        window.center()
        window.delegate = self

        let body = NSStackView()
        body.orientation = .vertical
        body.alignment = .leading
        body.spacing = 8
        func label(_ text: String) -> NSTextField { NSTextField(wrappingLabelWithString: text) }
        func heading(_ text: String) -> NSTextField {
            let view = NSTextField(labelWithString: text)
            view.font = .systemFont(ofSize: 15, weight: .semibold)
            return view
        }
        func row(_ controls: NSView...) -> NSStackView {
            let view = NSStackView(views: controls)
            view.spacing = 8
            return view
        }
        func add(_ views: NSView...) {
            for view in views { body.addArrangedSubview(view) }
        }
        func section(_ title: String) {
            let line = NSBox()
            line.boxType = .separator
            add(line, heading(title))
            line.widthAnchor.constraint(equalTo: body.widthAnchor).isActive = true
        }
        let title = heading("Connect Dot through OpenAI")
        title.font = .systemFont(ofSize: 20, weight: .semibold)
        let intro = label("D-Bond connects outbound from this Mac. Vollna and other webhook senders still need their own reachable receiving URL.")
        let icon = NSImageView(image: Brand.app ?? NSImage())
        icon.imageScaling = .scaleProportionallyUpOrDown
        icon.widthAnchor.constraint(equalToConstant: 38).isActive = true
        icon.heightAnchor.constraint(equalToConstant: 38).isActive = true
        add(row(icon, title), intro)
        section("1. Connect this Mac")
        let account = label("Choose or create a tunnel for your ChatGPT workspace. Its runtime key needs Tunnels Read and Use.")
        idField.toolTip = "Use the tunnel ID associated with the same workspace as your Dot."
        keyField.toolTip = "Use a restricted runtime API key with Tunnels Read and Use."
        let platform = NSButton(title: "OpenAI tunnels…", target: self, action: #selector(openPlatform))
        let keys = NSButton(title: "API keys…", target: self, action: #selector(openKeys))
        install.target = self; install.action = #selector(installClient)
        idField.identifier = .init("openaiTunnel.id")
        idField.placeholderString = "Tunnel ID · tunnel_…"
        idField.setAccessibilityLabel("OpenAI tunnel ID")
        keyField.identifier = .init("openaiTunnel.key")
        keyField.setAccessibilityLabel("Restricted tunnel runtime key")
        start.target = self; start.action = #selector(configure)
        stop.target = self; stop.action = #selector(stopTunnel)
        add(account, row(platform, keys, install), idField, keyField, row(start, stop), clientStatus, tunnelStatus)
        section("2. Connect your existing Dot")
        let pluginGuide = label("In ChatGPT Plugins, choose Add custom MCP server → Tunnel. Select this tunnel, choose No authentication and name the plugin D-Bond. Limit the private plugin to people who may use your Mac bridge. Give your Dot the setup message once.")
        let openPlugin = NSButton(title: "Open ChatGPT Plugins…", target: self, action: #selector(openPlugins))
        subscribe.target = self; subscribe.action = #selector(copySubscription)
        subscription.identifier = .init("openaiTunnel.subscription")
        add(pluginGuide, row(openPlugin, subscribe), subscription)
        section("3. Route workflows to Dot")
        useRoute.target = self; useRoute.action = #selector(selectRoute)
        useRoute.identifier = .init("openaiTunnel.select")
        check.target = self; check.action = #selector(refresh)
        add(workflowRoute, row(check, useRoute))
        for field in [intro, account, pluginGuide, idField, keyField, clientStatus, tunnelStatus, subscription, workflowRoute] {
            field.widthAnchor.constraint(equalTo: body.widthAnchor).isActive = true
        }
        for field in [clientStatus, tunnelStatus, subscription, workflowRoute] {
            field.font = .systemFont(ofSize: 12)
            field.textColor = .secondaryLabelColor
            field.isSelectable = true
        }
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.drawsBackground = false
        let document = FlippedTunnelDocument()
        scroll.documentView = document
        document.translatesAutoresizingMaskIntoConstraints = false
        document.addSubview(body)
        body.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            document.leadingAnchor.constraint(equalTo: scroll.contentView.leadingAnchor),
            document.trailingAnchor.constraint(equalTo: scroll.contentView.trailingAnchor),

            body.leadingAnchor.constraint(equalTo: document.leadingAnchor, constant: 24),
            body.trailingAnchor.constraint(equalTo: document.trailingAnchor, constant: -24),
            body.topAnchor.constraint(equalTo: document.topAnchor, constant: 18),
            body.bottomAnchor.constraint(equalTo: document.bottomAnchor, constant: -20)
        ])
        window.contentView = scroll
        observer = NotificationCenter.default.addObserver(forName: OpenAITunnelRuntime.changed, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.render() }
        }
    }

    required init?(coder: NSCoder) { nil }

    func present() {
        idField.stringValue = runtime.tunnelID
        keyField.stringValue = ""
        feedback = ""
        copiedSetup = false
        render()
        NSApp.activate(ignoringOtherApps: true)
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
        refresh()
    }

    func windowDidBecomeKey(_ notification: Notification) { refresh() }
    func windowWillClose(_ notification: Notification) { keyField.stringValue = "" }

    @objc private func refresh() {
        guard !busy, !checking else { return }
        checking = true
        feedback = ""
        copiedSetup = false
        runtime.refreshStatus()
        render()
        connector.inspect { [weak self] result in
            guard let self else { return }
            self.checking = false
            switch result {
            case .success(let state): self.snapshot = state
            case .failure(let error): self.snapshot = nil; self.feedback = error.localizedDescription
            }
            self.render()
        }
    }

    private func render() {
        let ready = snapshot?.canSelectTunnelRoute(runtime.tunnelID) == true
        let clientMissing = runtime.executablePath == nil
        install.isHidden = !clientMissing
        install.isEnabled = !busy
        install.title = OpenAITunnelClientInstaller.homebrewURL == nil ? "Get Homebrew…" : "Install OpenAI client…"
        clientStatus.stringValue = clientMissing ? "Install the OpenAI tunnel client, then check the connection." : "OpenAI tunnel client installed."
        tunnelStatus.stringValue = feedback.isEmpty ? runtime.state : feedback
        if ready {
            subscription.stringValue = "ChatGPT connected through this tunnel. Dot’s standing subscription is verified."
        } else if snapshot?.subscribed == true {
            subscription.stringValue = "Dot currently uses another connection. Subscribing through the private plugin will replace that callback for all workflows on this connector."
        } else {
            subscription.stringValue = "Waiting for Dot to subscribe through the private plugin. Choose Check connection after Dot confirms."
        }
        let active = ready && connector.selectedRoute == "owned"
        workflowRoute.stringValue = active
            ? "Dot workflows use this connector. " + (runtime.ready ? "The tunnel is ready." : "Check tunnel availability.")
            : "Current workflow route: " + (connector.selectedRoute == "owned" ? "built-in Dot connector." : "legacy Dot connector.")
        keyField.placeholderString = runtime.configured ? "Runtime key · leave blank to reuse the saved key" : "Restricted runtime key · saved in login Keychain"
        subscribe.title = copiedSetup ? "Setup message copied" : "Copy Dot setup message"
        let edit = !busy && !runtime.running
        idField.isEnabled = edit; keyField.isEnabled = edit
        start.isEnabled = edit && !checking && runtime.executablePath != nil
        stop.isEnabled = !busy && runtime.enabled
        check.isEnabled = !busy && !checking
        subscribe.isEnabled = !busy && runtime.ready && snapshot?.servesTunnel(runtime.tunnelID) == true
        useRoute.isEnabled = !busy && !checking && runtime.ready && ready && !active
    }

    @objc private func configure() {
        guard !busy, !checking, !runtime.running else { return }
        let enteredKey = keyField.stringValue
        do {
            try runtime.configure(tunnelID: idField.stringValue, apiKey: enteredKey.isEmpty ? nil : enteredKey)
            keyField.stringValue = ""
        } catch { feedback = error.localizedDescription; render(); return }
        busy = true
        feedback = "Starting the Mac’s private Dot listener…"
        render()
        connector.configureTunnel(tunnelID: runtime.tunnelID) { [weak self] result in
            guard let self else { return }
            self.busy = false
            switch result {
            case .failure(let error): self.feedback = error.localizedDescription
            case .success(let status):
                self.snapshot = status
                do {
                    try self.runtime.start()
                    self.feedback = ""
                } catch { self.feedback = error.localizedDescription }
            }
            self.render()
        }
    }

    @objc private func stopTunnel() {
        guard !busy else { return }
        if connector.selectedRoute == "owned" && snapshot?.canSelectTunnelRoute(runtime.tunnelID) == true {
            let alert = Brand.alert()
            alert.messageText = "Stop this Dot tunnel?"
            alert.informativeText = "Dot uses this tunnel for workflow replies. Stopping it interrupts those replies until you start it again. Your service pairings and tunnel setup are saved."
            alert.addButton(withTitle: "Cancel")
            alert.addButton(withTitle: "Stop Tunnel")
            guard alert.runModal() == .alertSecondButtonReturn else { return }
        }
        runtime.stop(); feedback = ""; render()
    }

    @objc private func selectRoute() {
        guard !busy, runtime.ready else { return }
        busy = true
        feedback = ""
        render()
        connector.selectRoute("owned", requireTunnelID: runtime.tunnelID) { [weak self] result in
            guard let self else { return }
            self.busy = false
            switch result {
            case .success(let status): self.snapshot = status; self.onRouteChanged()
            case .failure(let error): self.snapshot = nil; self.feedback = error.localizedDescription
            }
            self.render()
        }
    }

    @objc private func copySubscription() {
        guard let state = snapshot, runtime.ready, state.servesTunnel(runtime.tunnelID) else { return }
        if state.subscribed && !state.canSelectTunnelRoute(runtime.tunnelID) {
            let alert = Brand.alert()
            alert.messageText = "Move Dot’s standing subscription?"
            alert.informativeText = "Giving Dot this message will switch this built-in connector’s wake callback to the private plugin as soon as Dot subscribes. Every service using that connector will share the change."
            alert.addButton(withTitle: "Cancel")
            alert.addButton(withTitle: "Copy Setup Message")
            guard alert.runModal() == .alertSecondButtonReturn else { return }
        }
        let message = "Use my private D-Bond plugin connected through OpenAI tunnel " + runtime.tunnelID + ". Keep one standing immediate subscription to app.message.created on channel d-bond. Renew it automatically if supported. Acknowledge requests first and return correlated replies through the same plugin."
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(message, forType: .string)
        copiedSetup = true
        render()
    }

    @objc private func openPlatform() { open("https://platform.openai.com/settings/organization/tunnels") }
    @objc private func openKeys() { open("https://platform.openai.com/settings/organization/api-keys") }
    @objc private func installClient() {
        guard !busy else { return }
        guard OpenAITunnelClientInstaller.homebrewURL != nil else {
            NSWorkspace.shared.open(OpenAITunnelClientInstaller.homebrewSetupURL)
            feedback = "After installing Homebrew, choose Check connection, then Install OpenAI client."
            render(); return
        }
        busy = true
        feedback = "Installing OpenAI’s tunnel client with Homebrew…"
        render()
        Task {
            do { _ = try await OpenAITunnelClientInstaller.install(); feedback = "" }
            catch { feedback = error.localizedDescription }
            busy = false; render()
        }
    }
    @objc private func openPlugins() { open("https://chatgpt.com/plugins") }
    private func open(_ value: String) { if let url = URL(string: value) { NSWorkspace.shared.open(url) } }
}

@MainActor
private final class FlippedTunnelDocument: NSView {
    override var isFlipped: Bool { true }
}
