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
    private let window: NSWindow
    private var scrollView: NSScrollView!
    private let target = NSPopUpButton()
    private let alias = NSTextField()
    private let status = NSTextField(labelWithString: "")
    private let send = NSButton(title: "Send setup note to listener", target: nil, action: nil)
    private let codexState = NSTextField(wrappingLabelWithString: "Checking Codex…")
    private let claudeState = NSTextField(wrappingLabelWithString: "Checking Claude Code…")
    private let desktopState = NSTextField(wrappingLabelWithString: "Checking Claude Desktop…")
    private let codexAction = NSButton(), claudeAction = NSButton(), desktopAction = NSButton()
    private let checkAction = NSButton()
    private var checkTask: Task<Void,Never>?
    private var saving = false
    private var updatingClaude = false
    private let setupNote = "The owner opened D-Bond's Agent Setup and sent this note to this exact paired connection. Use your D-Bond tools to inspect this connection and receive messages. This is setup information, not approval to submit, publish, or send anything else."

    static func show(hub: Hub) {
        if let current {
            current.refreshTargets(); current.checkInstalls()
            current.window.makeKeyAndOrderFront(nil)
            current.window.makeFirstResponder(current.window.contentView)
            current.scrollToTop()
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let controller = AgentSetup(hub: hub)
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

    private init(hub: Hub) {
        self.hub = hub
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 650, height: 660),
                          styleMask: [.titled, .closable, .resizable, .miniaturizable],
                          backing: .buffered, defer: false)
        super.init()
        // This cached controller owns the window across closes. AppKit must not release it.
        window.isReleasedWhenClosed = false
        window.title = "D-Bond · Agent Setup"
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
        let title = label("D-Bond · Connect agents", size: 19, bold: true)
        title.translatesAutoresizingMaskIntoConstraints = false
        let subtitle = label("Install tools and contact a verified listener", size: 12)
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

        for field in [codexState,claudeState,desktopState] {
            field.font = .systemFont(ofSize:13,weight:.medium)
            field.textColor = .labelColor; field.isSelectable = true
            field.widthAnchor.constraint(equalToConstant:505).isActive = true
        }
        configure(codexAction, title:"Checking…", action:#selector(installCodex), id:"codex")
        configure(claudeAction, title:"Checking…", action:#selector(installClaudeCode), id:"claude")
        configure(desktopAction, title:"Checking…", action:#selector(openClaudeDesktop), id:"desktop")
        configure(checkAction, title:"Check again", action:#selector(checkInstalls), id:"refresh")
        for item in [codexAction,claudeAction,desktopAction] { item.isEnabled = false }
        codexState.identifier = .init("setup.codex.status"); claudeState.identifier = .init("setup.claude.status")
        desktopState.identifier = .init("setup.desktop.status")
        let desktop = section("Claude Desktop", "Manage the local extension in Claude Desktop → Settings → Extensions.", controls:[desktopState,desktopAction])
        alias.placeholderString = "Existing pairing alias"
        alias.toolTip = "The alias of the Claude Code session you want to connect."
        let code = section("Claude Code", "The channel plugin uses this session’s D-Bond pairing. Claude Code controls wake permissions.", controls:[claudeState,alias,claudeAction])
        let codex = section("Codex", "Local Control manages connections and rules. Direct messaging lets a chat talk to its paired services.", controls:[codexState,codexAction])
        let generic = section("Other stdio agents", "Copy a local MCP configuration for an agent that supports stdio tools. This does not wake an idle chat.", controls: [button("Copy stdio configuration", action: #selector(copyStdio))])
        send.target = self; send.action = #selector(sendSetupNote)
        let recipients = section("Send to an active listener", "Only a paired, unpaused connection with an open direct message stream appears here. Delivery to that stream is not proof an AI turn started or answered.", controls: [target, button("Refresh listeners", action: #selector(refreshTargetsAction)), send, button("Copy setup note for a chosen chat", action: #selector(copySetupNote))])
        status.isSelectable = true
        status.maximumNumberOfLines = 0
        status.lineBreakMode = .byWordWrapping
        let intro = label("Installed on this Mac", size:15,bold:true)
        let checkRow = NSStackView(views:[intro,checkAction]); checkRow.spacing = 12
        stack.addArrangedSubview(checkRow)
        for view in [codex, code, desktop, generic, recipients, status] { stack.addArrangedSubview(view) }
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
            alias.widthAnchor.constraint(equalToConstant: 360),
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

    private func setAction(_ control: NSButton, _ title: String, enabled: Bool) {
        control.title = title; control.isEnabled = enabled && !saving
    }

    private func desktopInstall() {
        // Inspect only the known local MCPB manifest and its enablement field; never read account credentials.
        let root = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Claude")
        let id = "local.mcpb.ivg.d-bond", fm = FileManager.default
        let file = root.appendingPathComponent("Claude Extensions/" + id + "/manifest.json")
        let state = root.appendingPathComponent("Claude Extensions Settings/" + id + ".json")
        let shipped = Self.manifestVersion(Bundle.main.url(forResource:"plugin",withExtension:"json",subdirectory:"agent-installers/claude-code-marketplace/plugin/.claude-plugin"))
        let app = NSWorkspace.shared.urlForApplication(withBundleIdentifier:"com.anthropic.claudefordesktop")
        if let installed = Self.manifestVersion(file) {
            var label = "Installed " + installed
            if let data = try? Data(contentsOf:state), let obj = (try? JSONSerialization.jsonObject(with:data)) as? [String:Any], let enabled = obj["isEnabled"] as? Bool { label += enabled ? " · Enabled" : " · Disabled" }
            if let shipped, installed.compare(shipped, options:.numeric) == .orderedAscending {
                desktopState.stringValue = label + " · Version " + shipped + " available"
                setAction(desktopAction,"Open update in Claude…",enabled:app != nil)
            } else { desktopState.stringValue = label; setAction(desktopAction,"Already installed",enabled:false) }
        } else if fm.fileExists(atPath:file.path) {
            desktopState.stringValue = "Couldn’t verify the local extension."; setAction(desktopAction,"Check Claude settings",enabled:false)
        } else if app == nil {
            desktopState.stringValue = "Claude Desktop not found."; setAction(desktopAction,"Host unavailable",enabled:false)
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
        for b in [codexAction,claudeAction] { setAction(b,"Checking…",enabled:false) }
        checkAction.isEnabled = false; alias.isHidden = true
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
                self.codexState.stringValue = "Local Control: " + self.describe(c.plugin) + "\nDirect messaging: " + self.describe(c.directMCP)
                self.codexState.toolTip = [c.plugin.source,c.directMCP.source].compactMap{$0}.joined(separator:" · ")
                if !Self.safeToInstall(c.plugin) || !Self.safeToInstall(c.directMCP) {
                    self.setAction(self.codexAction,"Review Codex settings",enabled:false)
                } else if c.plugin.status == .missing || c.directMCP.status == .missing {
                    let title = c.plugin.status == .missing ? (c.directMCP.status == .missing ? "Install Codex tools…" : "Install Local Control…") : "Add direct messaging…"
                    self.setAction(self.codexAction,title,enabled:true)
                } else if c.plugin.updateAvailable {
                    self.setAction(self.codexAction,"Update Local Control…",enabled:true)
                } else { self.setAction(self.codexAction,"Already installed",enabled:false) }
            } else {
                self.codexState.stringValue = "The bundled Codex adapter is missing."
                self.setAction(self.codexAction,"Installer unavailable",enabled:false)
            }
            self.claudeState.stringValue = self.describe(a); self.claudeState.toolTip = a.source
            self.updatingClaude = a.status == .installed && a.updateAvailable
            self.alias.isHidden = a.status != .missing
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
        let value = alias.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let update = updatingClaude
        guard update || (!value.isEmpty && value.utf8.count <= 64 &&
              value.unicodeScalars.allSatisfy({ CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-").contains($0) })) else {
            show("Pairing alias required", "Enter an existing local alias using letters, numbers, period, underscore or hyphen.")
            return
        }
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
                    guard current.status == .missing, Self.installClaudeCodePlugin(cli:cli,marketplace:marketplace,alias:value) else { return false }
                }
                let verified = AgentInstallState.claudeCode(expectedVersion:expected)
                return verified.status == .installed && verified.enabled == true && !verified.updateAvailable
            }.value
            guard let self else { return }
            self.endInstall()
            if result { self.status.stringValue = update ? "Claude Code plugin updated. Restart Claude Code to load it; your pairing was kept." : "Claude Code plugin installed. Use the chosen alias in your intended session." }
            else { self.show("Claude Code setup needs attention", "The requested version or configuration could not be verified. Review the plugin in Claude Code; your other connections were left alone.") }
        }
    }

    nonisolated private static func installClaudeCodePlugin(cli: URL, marketplace: URL, alias: String) -> Bool {
        guard let result = json(cli, ["plugin", "install", "d-bond", "--marketplace", marketplace.path,
                                      "--config", "alias=\(alias)", "--scope", "user", "--json"]) as? [String: Any],
              result["outcome"] as? String == "ok", result["configApplied"] as? Bool == true,
              result["pluginId"] as? String == "d-bond@d-bond-local" else { return false }
        guard let check = json(cli, ["plugin", "configure", "d-bond@d-bond-local", "--json"]) as? [String: Any],
              let configured = check["configured"] as? [String], configured.contains("alias"),
              let inputs = check["inputs"] as? [String: Any], inputs["alias"] as? String == alias else { return false }
        return true
    }

    @objc private func installCodex() {
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
        guard safeToInstall(initial.plugin), safeToInstall(initial.directMCP) else {
            return "Existing Codex settings could not be verified or are disabled. No settings were changed."
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
        if initial.directMCP.status == .missing {
            guard execute(cli,["mcp","add","d-bond-agent","--","/bin/sh",launcher.path]) == 0 else {
                return "Could not add direct messaging. Check the status of Local Control above."
            }
        }
        let after = AgentInstallState.codex(expectedVersion:expected,expectedLauncher:launcher)
        guard after.plugin.status == .installed, after.plugin.enabled == true, !after.plugin.updateAvailable,
              after.directMCP.status == .installed, after.directMCP.enabled == true else {
            return "Could not verify the current version of both Codex tools. Check the installed marketplace for an update; D-Bond kept your existing configuration."
        }
        return "Codex Local Control and direct messaging are installed and enabled. Open a new chat if needed for tool discovery."
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
