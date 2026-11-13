import AppKit
import DBondCore
import Foundation

/// Owner-initiated connector setup. Nothing installs or sends until a button is clicked.
@MainActor
final class AgentSetup: NSObject, NSWindowDelegate {
    private static var current: AgentSetup?
    private let hub: Hub
    private let window: NSWindow
    private let target = NSPopUpButton()
    private let alias = NSTextField()
    private let status = NSTextField(labelWithString: "")
    private let send = NSButton(title: "Send setup note to listener", target: nil, action: nil)
    private let setupNote = "The owner opened D-Bond's Agent Setup and sent this note to this exact paired connection. Use your D-Bond tools to inspect this connection and receive messages. This is setup information, not approval to submit, publish, or send anything else."

    static func show(hub: Hub) {
        if let current { current.refreshTargets(); current.window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true); return }
        let controller = AgentSetup(hub: hub)
        current = controller
        controller.refreshTargets()
        controller.window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private init(hub: Hub) {
        self.hub = hub
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 650, height: 660),
                          styleMask: [.titled, .closable, .resizable, .miniaturizable],
                          backing: .buffered, defer: false)
        super.init()
        window.title = "Connect agents"
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
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.translatesAutoresizingMaskIntoConstraints = false
        let content = NSView()
        content.translatesAutoresizingMaskIntoConstraints = false
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false

        let desktop = section("Claude Desktop", "Open the bundled local MCP extension in Claude Desktop, then approve and enable it there. Opening the installer does not complete Claude's approval.", controls: [button("Open .mcpb installer…", action: #selector(openClaudeDesktop))])
        alias.placeholderString = "Existing Claude Code pairing alias"
        alias.toolTip = "One durable local alias for the intended Claude Code session. No credential is entered here."
        let code = section("Claude Code", "Install the local channel plugin for future local sessions. Claude Code still requires its own channel opt-in and a configured pairing alias.", controls: [alias, button("Install Claude Code plugin…", action: #selector(installClaudeCode))])
        let codex = section("Codex", "Install D-Bond local controls through Codex's plugin manager and direct messaging through Codex's MCP settings. A new chat may be needed for tool discovery.", controls: [button("Install Codex tools…", action: #selector(installCodex))])
        let generic = section("Other stdio agents", "Copy a local MCP configuration for an agent that supports stdio tools. This does not wake an idle chat.", controls: [button("Copy stdio configuration", action: #selector(copyStdio))])
        send.target = self; send.action = #selector(sendSetupNote)
        let recipients = section("Send to an active listener", "Only a paired, unpaused connection with an open direct message stream appears here. Delivery to that stream is not proof an AI turn started or answered.", controls: [target, button("Refresh listeners", action: #selector(refreshTargetsAction)), send, button("Copy setup note for a chosen chat", action: #selector(copySetupNote))])
        status.isSelectable = true
        status.maximumNumberOfLines = 0
        status.lineBreakMode = .byWordWrapping
        let intro = label("Install or connect the agent you choose. Nothing is sent on opening this window.", size: 14)
        intro.widthAnchor.constraint(equalToConstant: 510).isActive = true
        stack.addArrangedSubview(intro)
        for view in [desktop, code, codex, generic, recipients, status] { stack.addArrangedSubview(view) }
        content.addSubview(stack)
        scroll.documentView = content
        window.contentView = scroll
        NSLayoutConstraint.activate([
            scroll.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor),
            scroll.topAnchor.constraint(equalTo: window.contentView!.topAnchor),
            scroll.bottomAnchor.constraint(equalTo: window.contentView!.bottomAnchor),
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
        guard !value.isEmpty, value.utf8.count <= 80,
              value.unicodeScalars.allSatisfy({ CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-").contains($0) }) else {
            show("Pairing alias required", "Enter an existing local alias using letters, numbers, period, underscore or hyphen.")
            return
        }
        guard let marketplace = Bundle.main.url(forResource: "claude-code-marketplace", withExtension: nil, subdirectory: "agent-installers"),
              let cli = executable("claude") else { show("Installer unavailable", "The packaged plugin or Claude Code CLI is missing."); return }
        status.stringValue = "Installing through Claude Code…"
        Task { [weak self] in
            let installed = await Task.detached { Self.installClaudeCodePlugin(cli: cli, marketplace: marketplace, alias: value) }.value
            guard let self else { return }
            if installed { self.status.stringValue = "Claude Code plugin and pairing alias are configured. Start or resume the intended session with its required channel opt-in." }
            else { self.show("Claude Code setup needs attention", "The plugin or pairing alias could not be verified. Inspect it in Claude Code's plugin manager; no setup note was sent.") }
        }
    }

    nonisolated private static func installClaudeCodePlugin(cli: URL, marketplace: URL, alias: String) -> Bool {
        guard let result = json(cli, ["plugin", "install", "d-bond", "--marketplace", marketplace.path,
                                      "--config", "alias=\(alias)", "--scope", "user", "--json"]) as? [String: Any],
              result["outcome"] as? String == "ok", result["configApplied"] as? Bool == true,
              result["pluginId"] as? String == "d-bond@d-bond-local" else { return false }
        guard let check = json(cli, ["plugin", "configure", "d-bond@d-bond-local", "--json"]) as? [String: Any],
              let configured = check["configured"] as? [String: Any], configured["alias"] != nil else { return false }
        return true
    }

    @objc private func installCodex() {
        guard let marketplace = Bundle.main.url(forResource: "codex-marketplace", withExtension: nil, subdirectory: "agent-installers"),
              let launcher = Bundle.main.url(forResource: "launch", withExtension: "sh", subdirectory: "agent-installers/direct-stdio"),
              let cli = executable("codex") else { show("Installer unavailable", "The packaged plugin or Codex CLI is missing."); return }
        status.stringValue = "Checking Codex plugin and MCP settings…"
        Task { [weak self] in
            let result = await Task.detached { Self.installCodexTools(cli: cli, marketplace: marketplace, launcher: launcher) }.value
            self?.status.stringValue = result
            if result.hasPrefix("Could not") || result.hasPrefix("Existing") { self?.show("Codex setup needs attention", result) }
        }
    }

    nonisolated private static func installCodexTools(cli: URL, marketplace: URL, launcher: URL) -> String {
        guard let mcp = json(cli, ["mcp", "list", "--json"]) as? [[String: Any]] else {
            return "Could not read Codex MCP settings; no settings were changed."
        }
        let name = "d-bond-agent"
        if let row = mcp.first(where: { $0["name"] as? String == name }) {
            guard let transport = row["transport"] as? [String: Any],
                  transport["type"] as? String == "stdio",
                  transport["command"] as? String == "/bin/sh",
                  transport["args"] as? [String] == [launcher.path] else {
                return "Existing Codex MCP server named d-bond-agent has a different command. D-Bond left it unchanged."
            }
        }
        guard let plugins = json(cli, ["plugin", "list", "--json"]) as? [String: Any],
              let installed = plugins["installed"] as? [[String: Any]] else {
            return "Could not read Codex plugins; no settings were changed."
        }
        let pluginID = "d-bond-local-control@d-bond-local"
        if !installed.contains(where: { $0["pluginId"] as? String == pluginID && $0["installed"] as? Bool == true }) {
            guard let listing = json(cli, ["plugin", "marketplace", "list", "--json"]) as? [String: Any],
                  let markets = listing["marketplaces"] as? [[String: Any]] else {
                return "Could not read Codex marketplaces; no settings were changed."
            }
            if let existing = markets.first(where: { $0["name"] as? String == "d-bond-local" }) {
                guard existing["root"] as? String == marketplace.path else {
                    return "Existing d-bond-local marketplace points to another location. D-Bond left it unchanged; use Codex's plugin manager to review it."
                }
            } else if execute(cli, ["plugin", "marketplace", "add", marketplace.path, "--json"]) != 0 {
                return "Could not add the bundled Codex marketplace. No direct MCP setting was changed."
            }
            guard execute(cli, ["plugin", "add", pluginID, "--json"]) == 0 else {
                return "Could not install the Codex local control plugin. No direct MCP setting was changed."
            }
        }
        if !mcp.contains(where: { $0["name"] as? String == name }) {
            guard execute(cli, ["mcp", "add", name, "--", "/bin/sh", launcher.path]) == 0 else {
                return "Could not register the D-Bond direct messaging MCP server. The local control plugin may already be installed."
            }
        }
        guard let after = json(cli, ["mcp", "list", "--json"]) as? [[String: Any]],
              after.contains(where: { $0["name"] as? String == name && $0["enabled"] as? Bool == true }) else {
            return "Could not verify the D-Bond direct messaging MCP server after installation."
        }
        return "Codex local control plugin and direct messaging MCP server are configured. Open a new Codex chat if the tools do not appear in this one. An idle chat is not automatically woken."
    }

    nonisolated private static func json(_ cli: URL, _ args: [String]) -> Any? {
        let process = Process()
        process.executableURL = cli
        process.arguments = args
        process.environment = hostEnvironment()
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
