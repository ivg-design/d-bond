import AppKit
import DBondCore
import Foundation

private final class AgentSetupRootView: NSView {
    override var acceptsFirstResponder: Bool { true }
}

private final class AgentSetupDocumentView: NSView {
    override var isFlipped: Bool { true }
}

/// Owner-initiated connector setup. Nothing installs or sends until a button is clicked.
@MainActor
final class AgentSetup: NSObject, NSWindowDelegate {
    private static var current: AgentSetup?
    private let hub: Hub
    private var onDotSetup: (() -> Void)?
    private var onAddService: (() -> Void)?
    private let window: NSWindow
    private var scrollView: NSScrollView!
    private let target = NSPopUpButton()
    private let status = NSTextField(labelWithString: "")
    private let send = NSButton(title: "Send setup note to listener", target: nil, action: nil)
    private let codexState = NSTextField(wrappingLabelWithString: "Checking Codex…")
    private let claudeState = NSTextField(wrappingLabelWithString: "Checking Claude Code…")
    private let desktopState = NSTextField(wrappingLabelWithString: "Checking Claude Desktop…")
    private let codexAction = NSButton(), claudeAction = NSButton(), desktopAction = NSButton()
    private let checkAction = NSButton()
    private let heraldState = NSTextField(wrappingLabelWithString: "Checking Herald…")
    private let heraldAction = NSButton()
    private var checkTask: Task<Void,Never>?
    private var saving = false
    private var updatingClaude = false
    private let setupNote = "The owner opened D-Bond's Agent Setup and sent this note to this exact paired connection. Use your D-Bond tools to inspect this connection and receive messages. This is setup information, not approval to submit, publish, or send anything else."

    static func show(hub: Hub, onDotSetup: (() -> Void)? = nil, onAddService: (() -> Void)? = nil) {
        if let current {
            current.onDotSetup = onDotSetup
            current.onAddService = onAddService
            current.refreshTargets(); current.checkInstalls()
            current.window.makeKeyAndOrderFront(nil)
            current.window.makeFirstResponder(current.window.contentView)
            current.scrollToTop()
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let controller = AgentSetup(hub: hub, onDotSetup: onDotSetup, onAddService: onAddService)
        current = controller
        controller.refreshTargets(); controller.checkInstalls()
        controller.window.makeKeyAndOrderFront(nil)
        controller.window.makeFirstResponder(controller.window.contentView)
        controller.scrollToTop()
        NSApp.activate(ignoringOtherApps: true)
    }

    private func scrollToTop() {
        window.contentView?.layoutSubtreeIfNeeded()
        scrollView.contentView.scroll(to: .zero)
        scrollView.reflectScrolledClipView(scrollView.contentView)
    }

    private init(hub: Hub, onDotSetup: (() -> Void)?, onAddService: (() -> Void)?) {
        self.hub = hub
        self.onDotSetup = onDotSetup
        self.onAddService = onAddService
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 650, height: 660),
                          styleMask: [.titled, .closable, .resizable, .miniaturizable],
                          backing: .buffered, defer: false)
        super.init()
        // This cached controller owns the window across closes. AppKit must not release it.
        window.isReleasedWhenClosed = false
        window.title = "D-Bond · Connectors"
        window.minSize = NSSize(width: 580, height: 560)
        window.center()
        window.delegate = self
        buildWindow()
    }

    private func label(_ text: String, size: CGFloat = 13, bold: Bool = false) -> NSTextField {
        let field = NSTextField(wrappingLabelWithString: text)
        field.font = bold ? .boldSystemFont(ofSize: size) : .systemFont(ofSize: size)
        field.textColor = bold ? .labelColor : .secondaryLabelColor
        field.maximumNumberOfLines = 0
        return field
    }

    private func button(_ title: String, action: Selector) -> NSButton {
        let control = NSButton(title: title, target: self, action: action)
        control.bezelStyle = .rounded
        return control
    }

    private func section(_ title: String, _ detail: String, controls: [NSView]) -> NSStackView {
        let detailLabel = label(detail)
        detailLabel.widthAnchor.constraint(equalToConstant: 510).isActive = true
        let stack = NSStackView(views: [label(title, size: 16, bold: true), detailLabel] + controls)
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        stack.edgeInsets = NSEdgeInsets(top: 10, left: 0, bottom: 10, right: 0)
        return stack
    }

    private func buildWindow() {
        let root = AgentSetupRootView()
        let brand = NSImageView()
        brand.image = Brand.app ?? NSImage(systemSymbolName: "point.3.connected.trianglepath.dotted", accessibilityDescription: "D-Bond")
        brand.imageScaling = .scaleProportionallyUpOrDown
        brand.translatesAutoresizingMaskIntoConstraints = false
        let title = label("D-Bond · Connectors", size: 19, bold: true)
        title.translatesAutoresizingMaskIntoConstraints = false
        let subtitle = label("Install hosts, connect services, and set up agents", size: 12)
        subtitle.translatesAutoresizingMaskIntoConstraints = false
        let scroll = NSScrollView()
        scrollView = scroll
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.translatesAutoresizingMaskIntoConstraints = false
        let content = AgentSetupDocumentView()
        content.translatesAutoresizingMaskIntoConstraints = false
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false

        for field in [heraldState,codexState,claudeState,desktopState] {
            field.font = .systemFont(ofSize:13,weight:.medium)
            field.textColor = .labelColor; field.isSelectable = true
            field.widthAnchor.constraint(equalToConstant:505).isActive = true
        }
        configure(codexAction, title:"Checking…", action:#selector(installCodex), id:"codex")
        configure(claudeAction, title:"Checking…", action:#selector(installClaudeCode), id:"claude")
        configure(desktopAction, title:"Checking…", action:#selector(openClaudeDesktop), id:"desktop")
        configure(heraldAction, title:"Checking…", action:#selector(connectHerald), id:"herald")
        configure(checkAction, title:"Check again", action:#selector(checkInstalls), id:"refresh")
        for item in [heraldAction,codexAction,claudeAction,desktopAction] { item.isEnabled = false }
        heraldState.identifier = .init("setup.herald.status")
        codexState.identifier = .init("setup.codex.status"); claudeState.identifier = .init("setup.claude.status")
        desktopState.identifier = .init("setup.desktop.status")
        let herald = section("Herald notifications", "D-Bond delivers plain paired notifications to Herald's local API. Herald controls its own display, history, and sounds.", controls:[heraldState,heraldAction,button("Refresh Herald", action:#selector(refreshHeraldAction))])
        let dot = section("Dot", "Connect your existing Dot through its configured local transport. A connection does not prove a chat turn or a reply.", controls:[button("Configure Dot Connector…", action:#selector(configureDot))])
        let desktop = section("Claude Desktop", "Manage the local extension in Claude Desktop → Settings → Extensions.", controls:[desktopState,desktopAction])
        let code = section("Claude Code", "Each session enrolls its own D-Bond pairing after installation. Claude Code controls wake permissions.", controls:[claudeState,claudeAction])
        let codex = section("Codex", "Local Control manages connections and rules. Direct messaging lets a chat talk to its paired services.", controls:[codexState,codexAction])
        let generic = section("Other stdio agents", "Copy a local MCP configuration for an agent that supports stdio tools. This does not wake an idle chat.", controls: [button("Copy stdio configuration", action: #selector(copyStdio))])
        let vollna = section("Vollna job intake", "Use your existing authenticated webhook receiver and source ledger. The bundled sender example forwards one persisted job version; D-Bond does not install Vollna or start a watcher.", controls:[button("Create Vollna connection…", action:#selector(addService)),button("Reveal bundled Vollna example", action:#selector(revealVollnaExample))])
        let service = section("Other services", "Give an existing app or script a local endpoint and one-time credential. Configure that producer explicitly; creating a connection does not start one.", controls:[button("Create service connection…", action:#selector(addService))])
        send.target = self; send.action = #selector(sendSetupNote)
        let recipients = section("Send to an active listener", "Only a paired, unpaused connection with an open direct message stream appears here. Delivery to that stream is not proof an AI turn started or answered.", controls: [target, button("Refresh listeners", action: #selector(refreshTargetsAction)), send, button("Copy setup note for a chosen chat", action: #selector(copySetupNote))])
        status.isSelectable = true
        status.maximumNumberOfLines = 0
        status.lineBreakMode = .byWordWrapping
        let intro = label("Available connectors", size:15,bold:true)
        let checkRow = NSStackView(views:[intro,checkAction]); checkRow.spacing = 12
        stack.addArrangedSubview(checkRow)
        for view in [herald, dot, codex, code, desktop, generic, vollna, service, recipients, status] { stack.addArrangedSubview(view) }
        content.addSubview(stack)
        scroll.documentView = content
        root.addSubview(brand)
        root.addSubview(title)
        root.addSubview(subtitle)
        root.addSubview(scroll)
        window.contentView = root
        window.initialFirstResponder = root
        NSLayoutConstraint.activate([
            brand.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 24),
            brand.topAnchor.constraint(equalTo: root.topAnchor, constant: 16),
            brand.widthAnchor.constraint(equalToConstant: 40),
            brand.heightAnchor.constraint(equalToConstant: 40),
            title.leadingAnchor.constraint(equalTo: brand.trailingAnchor, constant: 12),
            title.topAnchor.constraint(equalTo: root.topAnchor, constant: 17),
            subtitle.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            subtitle.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 2),
            scroll.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            scroll.topAnchor.constraint(equalTo: root.topAnchor, constant: 72),
            scroll.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            content.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor),
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -24),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 20),
            stack.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -20),
            target.widthAnchor.constraint(equalToConstant: 510),
            status.widthAnchor.constraint(equalToConstant: 510)
        ])
    }

    private func configure(_ button: NSButton, title: String, action: Selector, id: String) {
        button.title = title; button.bezelStyle = .rounded; button.target = self; button.action = action
        button.identifier = .init("setup." + id)
    }

    fileprivate static func manifestVersion(_ url: URL?) -> String? {
        guard let url, let raw = try? Data(contentsOf:url), let value = (try? JSONSerialization.jsonObject(with:raw)) as? [String:Any] else { return nil }
        return value["version"] as? String
    }

    static func availableDesktopUpdate(installed: String, installers: URL?) -> String? {
        let manifest = installers?.appendingPathComponent("claude-desktop-manifest.json")
        guard let shipped = manifestVersion(manifest),
              installed.compare(shipped, options: .numeric) == .orderedAscending else { return nil }
        return shipped
    }

    private func setAction(_ control: NSButton, _ title: String, enabled: Bool) {
        control.title = title; control.isEnabled = enabled && !saving
    }

    private var heraldApp: URL? {
        let preferred = URL(fileURLWithPath:"/Applications/Herald.app",isDirectory:true)
        if Bundle(url:preferred)?.bundleIdentifier == "com.ivg.herald" { return preferred }
        return NSWorkspace.shared.urlForApplication(withBundleIdentifier:"com.ivg.herald")
    }

    private func heraldCatalogState() -> ConnectorCatalog.HeraldState {
        ConnectorCatalog.herald(hasApp: heraldApp != nil,
            routes: hub.snapshot().services.map { .init(name:$0.name,destination:$0.destination,paused:$0.paused) })
    }

    @objc private func refreshHeraldAction() { refreshHerald() }
    private func refreshHerald() {
        switch heraldCatalogState() {
        case .needsApp(let paired):
            heraldState.stringValue = paired.map { "\($0) is paired, but Herald is not installed. Install Herald before notifications can appear." }
                ?? "Herald is not installed. Download the verified public DMG, then drag Herald to Applications."
            setAction(heraldAction,"Install Herald…",enabled:true)
        case .needsConnection:
            heraldState.stringValue = "Herald is installed. No notification route is paired yet."
            setAction(heraldAction,"Connect Herald notifications",enabled:true)
        case .connected(let name):
            heraldState.stringValue = "Paired: \(name). Herald's runtime availability is checked when a notification is delivered."
            setAction(heraldAction,"Already paired",enabled:false)
        case .paused(let name):
            heraldState.stringValue = "\(name) is paused. Resume it in Connections."
            setAction(heraldAction,"Paused in D-Bond",enabled:false)
        case .multiple:
            heraldState.stringValue = "More than one Herald route exists. Review Connections before changing notification setup."
            setAction(heraldAction,"Review Connections",enabled:false)
        }
    }

    @objc private func connectHerald() {
        switch heraldCatalogState() {
        case .needsApp:
            setAction(heraldAction,"Downloading Herald…",enabled:false)
            status.stringValue = "Downloading and verifying the public Herald disk image…"
            Task { [weak self] in
                guard let self else { return }
                do {
                    let downloads = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Downloads",isDirectory:true)
                    let image = try await HeraldInstaller.download(to: downloads)
                    self.refreshHerald()
                    if NSWorkspace.shared.open(image) {
                        self.status.stringValue = "Opened verified Herald disk image. Drag Herald to Applications, then choose Refresh Herald. No notification was sent."
                    } else {
                        self.show("Verified download is ready", "Open \(image.path) in Finder and drag Herald to Applications.")
                    }
                } catch {
                    self.refreshHerald()
                    self.showHeraldDownloadFailure(error)
                }
            }
        case .needsConnection:
            do {
                _ = try hub.pair(name:"Herald / Notifications",destination:"herald",
                    sop:OwnerSOP.pairedHeraldNotifications(destination:"herald",enabled:true))
                refreshHerald()
                status.stringValue = "Herald / Notifications is paired locally. D-Bond will use Herald's local API when an authorized request arrives; no banner was sent."
            } catch {
                refreshHerald()
                show("Could not connect Herald",error.localizedDescription)
            }
        default: refreshHerald()
        }
    }

    private func showHeraldDownloadFailure(_ error: Error) {
        let alert = Brand.alert()
        alert.messageText = "Could not verify Herald download"
        alert.informativeText = error.localizedDescription + " You can get Herald from its official public release page."
        alert.addButton(withTitle:"Open official release page")
        alert.addButton(withTitle:"Cancel")
        if alert.runModal() == .alertFirstButtonReturn {
            _ = NSWorkspace.shared.open(ConnectorCatalog.heraldRelease)
        }
    }

    @objc private func configureDot() {
        guard let onDotSetup else {
            show("Dot connector unavailable", "Open D-Bond Settings to configure the local Dot connector.")
            return
        }
        onDotSetup()
    }

    @objc private func addService() {
        guard let onAddService else {
            show("Connection setup unavailable", "Open Connections and choose Add Connection to pair a service.")
            return
        }
        onAddService()
    }

    @objc private func revealVollnaExample() {
        guard let file = Bundle.main.resourceURL?.appendingPathComponent("agent-installers/examples/vollna/forward-job"),
              FileManager.default.isExecutableFile(atPath:file.path) else {
            show("Example unavailable", "Reinstall D-Bond from a complete app bundle containing its connector examples.")
            return
        }
        NSWorkspace.shared.activateFileViewerSelecting([file])
        status.stringValue = "Revealed the bundled Vollna sender example. Connect an existing authenticated watcher and retain its source ledger; D-Bond does not start one."
    }

    private func desktopInstall() {
        // Inspect only the known local MCPB manifest and its enablement field; never read account credentials.
        let root = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Claude")
        let id = "local.mcpb.ivg.d-bond", fm = FileManager.default
        let file = root.appendingPathComponent("Claude Extensions/" + id + "/manifest.json")
        let state = root.appendingPathComponent("Claude Extensions Settings/" + id + ".json")
        let installers = Bundle.main.resourceURL?.appendingPathComponent("agent-installers", isDirectory: true)
        let app = NSWorkspace.shared.urlForApplication(withBundleIdentifier:"com.anthropic.claudefordesktop")
        if let installed = Self.manifestVersion(file) {
            var label = "Installed " + installed
            if let data = try? Data(contentsOf:state), let obj = (try? JSONSerialization.jsonObject(with:data)) as? [String:Any], let enabled = obj["isEnabled"] as? Bool { label += enabled ? " · Enabled" : " · Disabled" }
            if let shipped = Self.availableDesktopUpdate(installed: installed, installers: installers) {
                desktopState.stringValue = label + " · Version " + shipped + " available"
                setAction(desktopAction,"Open update in Claude…",enabled:app != nil)
            } else { desktopState.stringValue = label; setAction(desktopAction,"Already installed",enabled:false) }
        } else if fm.fileExists(atPath:file.path) {
            desktopState.stringValue = "Couldn’t verify the local extension."; setAction(desktopAction,"Check Claude settings",enabled:false)
        } else if app == nil {
            desktopState.stringValue = "Claude Desktop is not installed. Get the macOS app, then return to install the bundled extension."
            setAction(desktopAction,"Get Claude Desktop…",enabled:true)
        } else {
            desktopState.stringValue = "D-Bond extension not detected."; setAction(desktopAction,"Open extension installer…",enabled:true)
        }
    }

    private var launcher: URL? { Bundle.main.url(forResource:"launch",withExtension:"sh",subdirectory:"agent-installers/direct-stdio") }
    private var codexVersion: String {
        Self.manifestVersion(Bundle.main.url(forResource:"plugin",withExtension:"json",subdirectory:"agent-installers/codex-marketplace/plugins/d-bond-local-control/.codex-plugin")) ?? ""
    }
    private var claudeVersion: String {
        Self.manifestVersion(Bundle.main.url(forResource:"plugin",withExtension:"json",subdirectory:"agent-installers/claude-code-marketplace/plugin/.claude-plugin")) ?? ""
    }

    nonisolated private static func safeToInstall(_ c: AgentInstallState.Component) -> Bool {
        switch c.status {
        case .missing: return true
        case .installed: return c.enabled == true
        default: return false
        }
    }

    nonisolated private static func pluginHasDirect(_ c: AgentInstallState.Component) -> Bool {
        guard c.status == .installed, c.enabled == true, let version = c.version else { return false }
        return version.compare("0.1.3", options: .numeric) != .orderedAscending
    }

    private func describe(_ item: AgentInstallState.Component) -> String {
        switch item.status {
        case .unavailable: return "Couldn’t read host settings"
        case .missing: return "Not installed"
        case .conflict: return "Different configuration · left unchanged"
        case .installed:
            var text = "Installed"
            if let v = item.version { text += " " + v }
            if let enabled = item.enabled { text += enabled ? " · Enabled" : " · Disabled in host" }
            if item.updateAvailable, let v = item.expectedVersion { text += " · " + v + " available" }
            return text
        }
    }

    @objc private func checkInstalls() {
        guard !saving, checkTask == nil else { return }
        refreshHerald()
        for b in [codexAction,claudeAction] { setAction(b,"Checking…",enabled:false) }
        checkAction.isEnabled = false
        desktopInstall()
        let resource = launcher, cv = codexVersion, av = claudeVersion
        checkTask = Task { [weak self] in
            async let codex = Task.detached(priority:.utility) {
                resource.map { AgentInstallState.codex(expectedVersion:cv,expectedLauncher:$0) }
            }.value
            async let claude = Task.detached(priority:.utility) { AgentInstallState.claudeCode(expectedVersion:av) }.value
            let (c,a) = await (codex,claude)
            guard !Task.isCancelled, let self else { return }
            self.checkTask = nil; self.checkAction.isEnabled = true
            if let c {
                let direct = Self.pluginHasDirect(c.plugin) ? "Included in Local Control" :
                    (c.directMCP.status == .installed ? "Legacy separate server: " + self.describe(c.directMCP) : "Requires Local Control 0.1.3")
                self.codexState.stringValue = "Local Control: " + self.describe(c.plugin) + "\nDirect messaging: " + direct
                self.codexState.toolTip = [c.plugin.source,c.directMCP.source].compactMap{$0}.joined(separator:" · ")
                if self.executable("codex") == nil {
                    self.codexState.stringValue = "Codex is not installed. Get the official app or CLI, then return to install D-Bond tools."
                    self.setAction(self.codexAction,"Get Codex…",enabled:true)
                } else if !Self.safeToInstall(c.plugin) {
                    self.setAction(self.codexAction,"Review Codex settings",enabled:false)
                } else if c.plugin.status == .missing {
                    self.setAction(self.codexAction,"Install Codex tools…",enabled:true)
                } else if c.plugin.updateAvailable {
                    self.setAction(self.codexAction,"Update Local Control…",enabled:true)
                } else if Self.pluginHasDirect(c.plugin) {
                    self.setAction(self.codexAction,"Already installed",enabled:false)
                } else { self.setAction(self.codexAction,"Review plugin version",enabled:false) }
            } else {
                self.codexState.stringValue = "The bundled Codex adapter is missing."
                self.setAction(self.codexAction,"Installer unavailable",enabled:false)
            }
            self.claudeState.stringValue = self.describe(a); self.claudeState.toolTip = a.source
            self.updatingClaude = a.status == .installed && a.updateAvailable
            if self.executable("claude") == nil {
                self.claudeState.stringValue = "Claude Code CLI is not installed. Get it first, then return to install the D-Bond plugin."
                self.setAction(self.claudeAction,"Get Claude Code…",enabled:true)
            } else {
                switch a.status {
                case .missing: self.setAction(self.claudeAction,"Install Claude Code plugin…",enabled:true)
                case .installed where a.enabled == false: self.setAction(self.claudeAction,"Disabled in Claude Code",enabled:false)
                case .installed where a.updateAvailable && a.source == "Claude Code marketplace: d-bond-local · user":
                    self.setAction(self.claudeAction,"Update Claude Code plugin…",enabled:true)
                case .installed where a.updateAvailable: self.setAction(self.claudeAction,"Update in Claude Code",enabled:false)
                case .installed: self.setAction(self.claudeAction,"Already installed",enabled:false)
                default: self.setAction(self.claudeAction,"Review Claude Code settings",enabled:false)
                }
            }
        }
    }

    private func beginInstall() {
        saving = true; checkTask?.cancel(); checkTask = nil; checkAction.isEnabled = false
        for b in [codexAction,claudeAction,desktopAction] { b.isEnabled = false }
    }
    private func endInstall() { saving = false; checkInstalls() }

    @objc private func refreshTargetsAction() { refreshTargets() }
    private func refreshTargets() {
        let selected = target.selectedItem?.representedObject as? String
        target.removeAllItems()
        let services = hub.snapshot().services.filter { ["dot", "peer"].contains($0.destination) && !$0.paused && $0.directSubscribers > 0 }
        for service in services {
            target.addItem(withTitle: "\(service.name) · \(service.id)")
            target.lastItem?.representedObject = service.id
        }
        if let selected, let index = target.itemArray.firstIndex(where: { ($0.representedObject as? String) == selected }) { target.selectItem(at: index) }
        send.isEnabled = !services.isEmpty
        if services.isEmpty { status.stringValue = "No verified active direct listener. Copy the setup note into the exact chat you choose, or attach its supported listener first." }
    }

    @objc private func sendSetupNote() {
        guard let id = target.selectedItem?.representedObject as? String else { refreshTargets(); return }
        let eventID = "owner-setup-" + UUID().uuidString.lowercased()
        do {
            let sequence = try hub.sendOwnerDirectNote(to: id, id: eventID, text: setupNote)
            status.stringValue = "Sent to the active listener. Local receipt: \(eventID), sequence \(sequence). An agent turn and acknowledgement remain unverified."
        } catch {
            refreshTargets()
            show("Setup note was not sent", error.localizedDescription)
        }
    }

    @objc private func copySetupNote() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(setupNote, forType: .string)
        status.stringValue = "Copied setup note. Paste it into the exact chat you choose; D-Bond has not sent it."
    }

    @objc private func openClaudeDesktop() {
        if NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.anthropic.claudefordesktop") == nil {
            openHostPage(ConnectorCatalog.claudeDesktopDownload, name: "Claude Desktop")
            return
        }
        guard let file = Bundle.main.url(forResource: "d-bond-claude", withExtension: "mcpb", subdirectory: "agent-installers") else {
            show("Installer unavailable", "The bundled Claude Desktop installer is missing. Reinstall D-Bond from a complete build.")
            return
        }
        guard let claude = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.anthropic.claudefordesktop") else {
            NSWorkspace.shared.activateFileViewerSelecting([file])
            show("Claude Desktop unavailable", "Install Claude Desktop, then use Settings → Extensions → Advanced settings → Install Extension… to select the revealed .mcpb file.")
            return
        }
        NSWorkspace.shared.open([file], withApplicationAt: claude, configuration: NSWorkspace.OpenConfiguration()) { [weak self] _, error in
            DispatchQueue.main.async {
                if let error { self?.show("Could not open installer", error.localizedDescription) }
                else { self?.show("Finish in Claude Desktop", "Approve and enable the extension in Claude Desktop. D-Bond has only opened its installer.") }
            }
        }
    }

    @objc private func installClaudeCode() {
        if executable("claude") == nil {
            openHostPage(ConnectorCatalog.claudeCodeSetup, name: "Claude Code")
            return
        }
        let update = updatingClaude
        guard let marketplace = Bundle.main.url(forResource: "claude-code-marketplace", withExtension: nil, subdirectory: "agent-installers"),
              let cli = executable("claude") else { show("Installer unavailable", "The packaged plugin or Claude Code CLI is missing."); return }
        beginInstall()
        status.stringValue = update ? "Updating Claude Code…" : "Installing through Claude Code…"
        let expected = claudeVersion
        Task { [weak self] in
            let result = await Task.detached { () -> Bool in
                let current = AgentInstallState.claudeCode(expectedVersion:expected)
                if update {
                    guard current.status == .installed, current.enabled == true, current.updateAvailable,
                          current.source == "Claude Code marketplace: d-bond-local · user" else { return false }
                    guard Self.execute(cli, ["plugin","update","d-bond@d-bond-local","--scope","user","--json"]) == 0 else { return false }
                } else {
                    guard current.status == .missing, Self.installClaudeCodePlugin(cli:cli,marketplace:marketplace) else { return false }
                }
                let verified = AgentInstallState.claudeCode(expectedVersion:expected)
                return verified.status == .installed && verified.enabled == true && !verified.updateAvailable
            }.value
            guard let self else { return }
            self.endInstall()
            if result { self.status.stringValue = update ? "Claude Code plugin updated. Restart Claude Code to load it; existing session pairings were kept." : "Claude Code plugin installed. Enroll from each intended session to start its own connection." }
            else { self.show("Claude Code setup needs attention", "The requested version or configuration could not be verified. Review the plugin in Claude Code; your other connections were left alone.") }
        }
    }

    nonisolated private static func installClaudeCodePlugin(cli: URL, marketplace: URL) -> Bool {
        guard let result = json(cli, ["plugin", "install", "d-bond", "--marketplace", marketplace.path,
                                      "--scope", "user", "--json"]) as? [String: Any],
              result["outcome"] as? String == "ok",
              result["pluginId"] as? String == "d-bond@d-bond-local" else { return false }
        return true
    }

    @objc private func installCodex() {
        if executable("codex") == nil {
            openHostPage(ConnectorCatalog.codexDownload, name: "Codex")
            return
        }
        guard let marketplace = Bundle.main.url(forResource: "codex-marketplace", withExtension: nil, subdirectory: "agent-installers"),
              let launcher = Bundle.main.url(forResource: "launch", withExtension: "sh", subdirectory: "agent-installers/direct-stdio"),
              let cli = executable("codex") else { show("Installer unavailable", "The packaged plugin or Codex CLI is missing."); return }
        beginInstall()
        let expected = codexVersion
        status.stringValue = "Checking Codex plugin and MCP settings…"
        Task { [weak self] in
            let result = await Task.detached { Self.installCodexTools(cli: cli, marketplace: marketplace, launcher: launcher, expected: expected) }.value
            self?.endInstall()
            self?.status.stringValue = result
            if result.hasPrefix("Could not") || result.hasPrefix("Existing") { self?.show("Codex setup needs attention", result) }
        }
    }

    nonisolated private static func installCodexTools(cli: URL, marketplace: URL, launcher: URL, expected: String) -> String {
        let initial = AgentInstallState.codex(expectedVersion:expected,expectedLauncher:launcher)
        guard safeToInstall(initial.plugin) else {
            return "Existing Codex plugin settings could not be verified or are disabled. No settings were changed."
        }
        if initial.plugin.status == .missing || initial.plugin.updateAvailable {
            let existingGit = initial.plugin.source == "Codex marketplace: dbond-github"
            var pluginID = "d-bond-local-control@d-bond-local"
            if initial.plugin.status == .installed {
                if existingGit { pluginID = "d-bond-local-control@dbond-github" }
                // Reuse its own marketplace. Never replace a user's source or install a duplicate.
            } else {
                guard let listing = json(cli, ["plugin","marketplace","list","--json"]) as? [String:Any],
                      let markets = listing["marketplaces"] as? [[String:Any]] else { return "Could not read Codex marketplaces. No settings were changed." }
                if let existing = markets.first(where:{ $0["name"] as? String == "d-bond-local" }) {
                    guard existing["root"] as? String == marketplace.path else { return "Existing d-bond-local marketplace uses another location. Review it in Codex; it was left unchanged." }
                } else if execute(cli,["plugin","marketplace","add",marketplace.path,"--json"]) != 0 { return "Could not add the bundled Codex marketplace." }
            }
            guard execute(cli,["plugin","add",pluginID,"--json"]) == 0 else { return "Could not install the requested Codex plugin from its marketplace." }
        }
        let after = AgentInstallState.codex(expectedVersion:expected,expectedLauncher:launcher)
        guard pluginHasDirect(after.plugin), !after.plugin.updateAvailable else {
            return "Could not verify Local Control 0.1.3 or newer. Check the installed marketplace; D-Bond kept your existing direct server unchanged."
        }
        return "Codex Local Control includes direct and Dot messaging. Existing d-bond-agent settings were left unchanged. Open a new chat if needed for tool discovery."
    }

    nonisolated private static func json(_ cli: URL, _ args: [String]) -> Any? {
        let process = Process()
        process.executableURL = cli
        process.arguments = args
        process.environment = hostEnvironment()
        process.standardInput = FileHandle.nullDevice
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            return process.terminationStatus == 0 ? try JSONSerialization.jsonObject(with: data) : nil
        } catch { return nil }
    }

    @objc private func copyStdio() {
        guard let launcher = Bundle.main.url(forResource: "launch", withExtension: "sh", subdirectory: "agent-installers/direct-stdio") else {
            show("Configuration unavailable", "The direct stdio adapter is not bundled in this build.")
            return
        }
        let config: [String: Any] = ["mcpServers": ["d-bond": ["command": "/bin/sh", "args": [launcher.path]]]]
        guard let data = try? JSONSerialization.data(withJSONObject: config, options: [.prettyPrinted, .sortedKeys]),
              let text = String(data: data, encoding: .utf8) else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        status.stringValue = "Copied local stdio configuration. Paste it into the chosen agent's MCP settings, then verify its tools in that agent."
    }

    private func executable(_ name: String) -> URL? {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let homeLocal = "\(home)/.local/bin/\(name)"
        let candidates = name == "codex"
            ? ["/opt/homebrew/bin/\(name)", "/usr/local/bin/\(name)", homeLocal, "/usr/bin/\(name)"]
            : [homeLocal, "/opt/homebrew/bin/\(name)", "/usr/local/bin/\(name)", "/usr/bin/\(name)"]
        for path in candidates {
            if FileManager.default.isExecutableFile(atPath: path) { return URL(fileURLWithPath: path) }
        }
        return nil
    }

    private func openHostPage(_ url: URL, name: String) {
        guard NSWorkspace.shared.open(url) else {
            show("Could not open \(name) setup", "Open \(url.absoluteString) in your browser.")
            return
        }
        status.stringValue = "Opened official \(name) setup. Install the host, then choose Check again in D-Bond."
    }

    nonisolated private static func execute(_ executable: URL, _ args: [String]) -> Int32 {
        let process = Process()
        process.executableURL = executable
        process.arguments = args
        process.environment = hostEnvironment()
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do { try process.run(); process.waitUntilExit(); return process.terminationStatus }
        catch { return -1 }
    }

    nonisolated private static func hostEnvironment() -> [String: String] {
        var values = ProcessInfo.processInfo.environment
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        values["PATH"] = ["\(home)/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"].joined(separator: ":")
        return values
    }

    private func show(_ title: String, _ detail: String) {
        let alert = Brand.alert()
        alert.messageText = title
        alert.informativeText = detail
        alert.runModal()
    }
}
