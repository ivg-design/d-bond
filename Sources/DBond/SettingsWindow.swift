import AppKit
import ServiceManagement
import DBondCore
import UniformTypeIdentifiers

enum GeneralPreferences {
    static let openConnectionsAtLaunch = "openConnectionsAtLaunch"

    static func shouldOpenConnections(isFirstSetup: Bool, arguments: [String], defaults: UserDefaults = .standard) -> Bool {
        isFirstSetup || arguments.contains("--connections") || defaults.bool(forKey: openConnectionsAtLaunch)
    }

    static func versionDescription(bundle: Bundle = .main) -> String {
        guard let version = bundle.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String,
              let build = bundle.object(forInfoDictionaryKey: "CFBundleVersion") as? String else {
            return "Development build"
        }
        return "Version \(version) (build \(build))"
    }
}

@MainActor
final class SettingsWindowController: NSWindowController {
    private let login = NSButton(checkboxWithTitle: "Start D-Bond at login", target: nil, action: nil)
    private let connections = NSButton(checkboxWithTitle: "Open Connections at launch", target: nil, action: nil)

    private let defaults: UserDefaults
    private let connector: DotConnector
    private let onConnectorChanged: () -> Void
    private let onHTTPSSetup: () -> Void
    private let publicOrigin: () -> String
    private let connectorStatus = NSTextField(wrappingLabelWithString: "Check status to inspect the built-in Dot connector.")
    private let configureDot = NSButton(title: "Configure…", target: nil, action: nil)
    private let refreshDot = NSButton(title: "Check status", target: nil, action: nil)
    private let startDot = NSButton(title: "Start", target: nil, action: nil)
    private let stopDot = NSButton(title: "Stop", target: nil, action: nil)
    private let useDot = NSButton(title: "Use Built-in", target: nil, action: nil)
    private let exportPlugin = NSButton(title: "Export Dot Plugin…", target: nil, action: nil)
    private let httpsSetup = NSButton(title: "Set Up HTTPS Access…", target: nil, action: nil)
    private let showSetup = NSButton(title: "Show OAuth Setup…", target: nil, action: nil)
    private let copyMCP = NSButton(title: "Copy MCP Address", target: nil, action: nil)
    private let openPlugin = NSButton(title: "Open ChatGPT Setup…", target: nil, action: nil)
    private let useLegacy = NSButton(title: "Use Legacy", target: nil, action: nil)
    private var latestStatus: DotConnectorStatus?

    init(defaults: UserDefaults = .standard, connector: DotConnector = DotConnector(), onConnectorChanged: @escaping () -> Void = {}, onHTTPSSetup: @escaping () -> Void = {}, publicOrigin: @escaping () -> String = { "" }) {
        self.defaults = defaults
        self.connector = connector
        self.onConnectorChanged = onConnectorChanged
        self.onHTTPSSetup = onHTTPSSetup
        self.publicOrigin = publicOrigin
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: 580),
                              styleMask: [.titled, .closable], backing: .buffered, defer: false)
        super.init(window: window)
        window.title = "D-Bond Settings"
        window.identifier = .init("dbond.settings.window")
        window.isReleasedWhenClosed = false
        window.center()

        let icon = NSImageView()
        icon.image = Brand.app
        icon.imageScaling = .scaleProportionallyUpOrDown
        icon.widthAnchor.constraint(equalToConstant: 48).isActive = true
        icon.heightAnchor.constraint(equalToConstant: 48).isActive = true
        let name = NSTextField(labelWithString: "D-Bond")
        name.font = .systemFont(ofSize: 20, weight: .semibold)
        let version = NSTextField(labelWithString: GeneralPreferences.versionDescription())
        version.textColor = .secondaryLabelColor
        version.isSelectable = true
        let identity = NSStackView(views: [name, version])
        identity.orientation = .vertical
        identity.alignment = .leading
        identity.spacing = 2
        let header = NSStackView(views: [icon, identity])
        header.spacing = 12

        let divider = NSBox()
        divider.boxType = .separator
        let group = NSTextField(labelWithString: "Startup")
        group.font = .systemFont(ofSize: 13, weight: .semibold)
        login.target = self
        login.action = #selector(setLogin)
        connections.identifier = .init("settings.openConnectionsAtLaunch")
        connections.target = self
        connections.action = #selector(setConnections)
        let preferences = NSStackView(views: [group, login, connections])
        preferences.orientation = .vertical
        preferences.alignment = .leading
        preferences.spacing = 9

        let dotHeading = NSTextField(labelWithString: "Dot connector")
        dotHeading.font = .systemFont(ofSize: 13, weight: .semibold)
        connectorStatus.identifier = .init("settings.dotConnector.status")
        connectorStatus.preferredMaxLayoutWidth = 510
        connectorStatus.isSelectable = true
        connectorStatus.font = .systemFont(ofSize: 12)
        let note = NSTextField(wrappingLabelWithString: "Configure your own public address and OAuth app. Connecting a Dot and verifying its subscription is a separate step. Existing legacy connections stay unchanged until you choose Use Built-in.")
        note.preferredMaxLayoutWidth = 510
        note.textColor = .secondaryLabelColor
        note.font = .systemFont(ofSize: 12)
        for (button, selector) in [(configureDot, #selector(configureConnector)), (refreshDot, #selector(inspectConnector)),
                                   (startDot, #selector(startConnector)), (stopDot, #selector(stopConnector)),
                                   (useDot, #selector(selectOwned)), (useLegacy, #selector(selectLegacy)),
                                   (showSetup, #selector(revealSetup)), (copyMCP, #selector(copyMCPAddress)),
                                   (openPlugin, #selector(openPluginSetup)), (httpsSetup, #selector(openHTTPSSetup)),
                                   (exportPlugin, #selector(exportDotPlugin))] {
            button.target = self
            button.action = selector
        }
        configureDot.identifier = .init("settings.dotConnector.configure")
        useDot.identifier = .init("settings.dotConnector.useOwned")
        let hostButtons = NSStackView(views: [configureDot, refreshDot, startDot, stopDot])
        hostButtons.spacing = 8
        let routeButtons = NSStackView(views: [useDot, useLegacy])
        routeButtons.spacing = 8
        let setupButtons = NSStackView(views: [showSetup, copyMCP, openPlugin])
        setupButtons.spacing = 8
        let dot = NSStackView(views: [dotHeading, connectorStatus, note, httpsSetup, hostButtons, setupButtons, exportPlugin, routeButtons])
        dot.orientation = .vertical
        dot.alignment = .leading
        dot.spacing = 9
        let content = NSStackView(views: [header, divider, preferences, dot])
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 16
        guard let root = window.contentView else { return }
        root.addSubview(content)
        content.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 24),
            content.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -24),
            content.topAnchor.constraint(equalTo: root.topAnchor, constant: 21),
            divider.widthAnchor.constraint(equalTo: content.widthAnchor)
        ])
    }

    required init?(coder: NSCoder) { nil }

    func present() {
        login.state = LoginRegistration.isEnabled ? .on : .off
        connections.state = defaults.bool(forKey: GeneralPreferences.openConnectionsAtLaunch) ? .on : .off
        NSApp.activate(ignoringOtherApps: true)
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
        inspectConnector()
    }

    func presentDotConnector() {
        present()
        window?.makeFirstResponder(configureDot)
    }

    private func busy(_ isBusy: Bool) {
        [configureDot, refreshDot, startDot, stopDot, useDot, useLegacy, showSetup, copyMCP, openPlugin, httpsSetup, exportPlugin].forEach { $0.isEnabled = !isBusy }
        if !isBusy {
            useDot.isEnabled = latestStatus?.canSelectOwnedRoute == true && connector.selectedRoute != "owned"
            startDot.isEnabled = latestStatus?.configured == true && latestStatus?.running != true
            stopDot.isEnabled = latestStatus?.running == true
            useLegacy.isEnabled = connector.selectedRoute == "owned"
            showSetup.isEnabled = latestStatus?.configured == true
            copyMCP.isEnabled = latestStatus?.mcpURL != nil
        }
    }

    private func completed(_ result: Result<DotConnectorStatus, Error>, refreshRoute: Bool = false) {
        switch result {
        case .success(let state):
            latestStatus = state
            if refreshRoute { onConnectorChanged() }
            let route = connector.selectedRoute == "owned" ? "Built-in" : "Legacy (unchanged)"
            let identity = state.identity?.connected == true ? "Dot connected" : "Dot not connected"
            let subscriptions = state.identity?.activeSubscriptions ?? 0
            connectorStatus.stringValue = "Route: \(route) · \(state.running ? "Running" : "Stopped")\n\(state.configured ? "Configured" : "Setup needed") · \(identity) · \(subscriptions) active subscription(s)" + (state.mcpURL.map { "\nMCP address: " + $0 } ?? "")
        case .failure(let error):
            connectorStatus.stringValue = error.localizedDescription
        }
        busy(false)
    }

    @objc private func inspectConnector() {
        busy(true)
        connector.inspect { [weak self] in self?.completed($0) }
    }

    @objc private func configureConnector() {
        let message = Brand.alert()
        message.messageText = "Configure the built-in Dot connector"
        message.informativeText = "First use Set Up HTTPS Access if you do not have a public hostname. Use a stable public HTTPS origin routed to this Mac’s restricted public ingress on port 18770 and the OAuth redirect URL from your ChatGPT plugin setup. Forward only to port 18770. Never publish the internal host on 18769 or the local D-Bond broker/control endpoints. Keep the registered HTTPS address stable. First setup prepares the connector runtime and may take a few minutes. Configuration starts the host for connection setup; it does not switch your current route."
        message.addButton(withTitle: "Configure & Start")
        message.addButton(withTitle: "Cancel")
        let publicURL = NSTextField(string: latestStatus?.publicURL ?? publicOrigin())
        publicURL.placeholderString = "Public origin, e.g. https://connector.example.com"
        publicURL.setAccessibilityLabel("Public HTTPS origin")
        let redirect = NSTextField(string: latestStatus?.redirectURI ?? "")
        redirect.placeholderString = "OAuth redirect URL from ChatGPT"
        redirect.setAccessibilityLabel("OAuth redirect URL")
        let fields = NSStackView(views: [publicURL, redirect])
        fields.orientation = .vertical
        fields.spacing = 10
        fields.frame = NSRect(x: 0, y: 0, width: 460, height: 65)
        fields.widthAnchor.constraint(equalToConstant: 460).isActive = true
        publicURL.widthAnchor.constraint(equalTo: fields.widthAnchor).isActive = true
        redirect.widthAnchor.constraint(equalTo: fields.widthAnchor).isActive = true
        message.accessoryView = fields
        guard message.runModal() == .alertFirstButtonReturn else { return }
        let origin = publicURL.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let callback = redirect.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard DotConnector.validSetup(publicURL: origin, redirectURI: callback) else {
            alert("Check the connector addresses", DotConnectorError.invalidURL.localizedDescription)
            return
        }
        busy(true)
        connectorStatus.stringValue = "Preparing the built-in connector… First setup may take a few minutes."
        connector.configure(publicURL: origin, redirectURI: callback) { [weak self] in self?.completed($0, refreshRoute: true) }
    }

    @objc private func openHTTPSSetup() { onHTTPSSetup() }

    @objc private func exportDotPlugin() {
        let choose = NSOpenPanel()
        choose.title = "Choose your connected app binding"
        choose.message = "Select your own app binding JSON. The exported optional skill contains this private app identity; keep it outside public repositories. Your connected OAuth app can register without this ZIP."
        choose.allowedContentTypes = [.json]
        choose.canChooseDirectories = false
        choose.allowsMultipleSelection = false
        guard choose.runModal() == .OK, let binding = choose.url else { return }
        let save = NSSavePanel()
        save.title = "Export your private Dot plugin"
        save.message = "Keep this app identity private. Choose a new ZIP filename outside your source repositories."
        save.allowedContentTypes = [.zip]
        save.nameFieldStringValue = "d-bond-dot-private.zip"
        guard save.runModal() == .OK, let output = save.url else { return }
        busy(true)
        connector.exportPlugin(binding: binding, output: output) { [weak self] result in
            guard let self else { return }
            self.busy(false)
            switch result {
            case .success: self.alert("Private Dot plugin exported", "Install the ZIP in your own ChatGPT account. Keep the app identity private; this optional skill does not create or authenticate the app connection.")
            case .failure(let error): self.alert("Could not export Dot plugin", error.localizedDescription)
            }
        }
    }

    @objc private func copyMCPAddress() {
        guard let address = latestStatus?.mcpURL else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(address, forType: .string)
    }

    @objc private func openPluginSetup() {
        let message = Brand.alert()
        message.messageText = "Connect your Dot in ChatGPT"
        message.informativeText = "Open ChatGPT Plugins and create or connect your own app using this connector’s MCP address and OAuth setup. Approve your account using the Owner approval key when asked. Then ask your Dot to subscribe to app.message.created on channel d-bond. Return here and Check status; Use Built-in becomes available after exactly one verified subscription."
        message.addButton(withTitle: "Open ChatGPT")
        message.addButton(withTitle: "Cancel")
        if message.runModal() == .alertFirstButtonReturn, let url = URL(string: "https://chatgpt.com/plugins") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc private func revealSetup() {
        busy(true)
        connector.setup { [weak self] result in
            guard let self else { return }
            self.busy(false)
            switch result {
            case .failure(let error): self.alert("Could not read OAuth setup", error.localizedDescription)
            case .success(let setup):
                let text = "MCP address: \(setup.mcpURL)\nClient ID: \(setup.clientID)\nOwner approval key: \(setup.ownerKey)"
                let message = Brand.alert()
                message.messageText = "Your private OAuth setup"
                message.informativeText = text + "\n\nKeep the owner approval key private. This personal OAuth connector does not require a client secret."
                message.addButton(withTitle: "Done")
                message.addButton(withTitle: "Copy Setup Securely")
                if message.runModal() == .alertSecondButtonReturn {
                    let pasteboard = NSPasteboard.general
                    pasteboard.clearContents()
                    pasteboard.setData(Data(text.utf8), forType: NSPasteboard.PasteboardType("org.nspasteboard.ConcealedType"))
                    pasteboard.setString(text, forType: .string)
                }
            }
        }
    }

    @objc private func startConnector() {
        busy(true)
        connector.start { [weak self] in self?.completed($0, refreshRoute: true) }
    }

    @objc private func stopConnector() {
        busy(true)
        connector.stop { [weak self] in self?.completed($0, refreshRoute: true) }
    }

    @objc private func selectOwned() {
        busy(true)
        connector.selectRoute("owned") { [weak self] in self?.completed($0, refreshRoute: true) }
    }

    @objc private func selectLegacy() {
        busy(true)
        connector.selectRoute("legacy") { [weak self] in self?.completed($0, refreshRoute: true) }
    }

    @objc private func setLogin() {
        do {
            try LoginRegistration.setEnabled(login.state == .on)
            if !LoginRegistration.usesSupervisor && SMAppService.mainApp.status == .requiresApproval {
                alert("Allow D-Bond to start at login", "Open System Settings → General → Login Items and allow D-Bond.")
            }
        } catch {
            alert("Couldn’t change Start at Login", error.localizedDescription)
        }
        login.state = LoginRegistration.isEnabled ? .on : .off
    }

    @objc private func setConnections() {
        defaults.set(connections.state == .on, forKey: GeneralPreferences.openConnectionsAtLaunch)
    }

    private func alert(_ title: String, _ detail: String) {
        guard let window else { return }
        let message = Brand.alert()
        message.messageText = title
        message.informativeText = detail
        message.beginSheetModal(for: window)
    }
}
