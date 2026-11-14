import AppKit
import DBondCore
import Darwin
import Foundation

@MainActor
private func require(_ condition: @autoclosure () -> Bool, _ detail: String) {
    guard condition() else { fatalError(detail) }
}

@MainActor
private func descendants(_ view: NSView) -> [NSView] {
    [view] + view.subviews.flatMap(descendants)
}

@main
struct SessionGuideFixture {
    @MainActor static func main() throws {
        NSApplication.shared.setActivationPolicy(.accessory)
        let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        let aliases = root.appendingPathComponent("aliases", isDirectory: true)
        try FileManager.default.createDirectory(at: aliases, withIntermediateDirectories: false,
                                                attributes: [.posixPermissions: 0o700])
        let id = UUID().uuidString.lowercased()
        let other = UUID().uuidString.lowercased()
        func save(_ alias: String, _ target: String, mode: mode_t = 0o600) throws {
            let file = aliases.appendingPathComponent(alias + ".json")
            let value: [String: Any] = ["endpoint": "http://127.0.0.1:18768/v1/services/\(target)/events",
                                        "secret": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="]
            let data = try JSONSerialization.data(withJSONObject: value)
            try data.write(to: file, options: .atomic)
            chmod(file.path, mode)
        }
        try save("other", other)
        require(SessionAlias.resolve(serviceID: id, directory: aliases) == .missing, "Wrong UUID matched alias")
        try save("claude_session", id)
        require(SessionAlias.resolve(serviceID: id, directory: aliases) == .unique("claude_session"), "Exact UUID alias missing")
        try save("unsafe_mode", id, mode: 0o644)
        let symlink = aliases.appendingPathComponent("symlink.json")
        try FileManager.default.createSymbolicLink(at: symlink, withDestinationURL: aliases.appendingPathComponent("claude_session.json"))
        require(SessionAlias.resolve(serviceID: id, directory: aliases) == .unique("claude_session"), "Unsafe file was accepted")
        try save("second_session", id)
        require(SessionAlias.resolve(serviceID: id, directory: aliases) == .ambiguous(["claude_session", "second_session"]), "Duplicate aliases guessed")
        require(SessionAlias.resolve(serviceID: id, directory: root.appendingPathComponent("absent")) == .missing,
                "Missing alias directory was not explicit")

        let hub = try Hub(directory: root.appendingPathComponent("hub", isDirectory: true), port: 0)
        _ = try hub.pair(name: "Claude / Fixture", destination: "peer")
        _ = try hub.pair(name: "Codex / Fixture", destination: "dot")
        _ = try hub.pair(name: "Dot / Dispatch", destination: "dot")
        _ = try hub.pair(name: "Vollna / Intake", destination: "dot")
        _ = try hub.pair(name: "Herald / Alerts", destination: "herald")
        _ = try hub.pair(name: "Codex / HTTP producer", destination: "dot", role: .workflow)
        _ = try hub.pair(name: "Claude / Extension", destination: "dot", chromeOrigin: "chrome-extension://" + String(repeating: "a", count: 32))
        let newName = try hub.pair(name: "Codex / Renamed", destination: "peer")
        try hub.rename(id: newName.id, name: "My chat")
        var setupCalls = 0
        var manageCalls = 0
        let controller = ConnectionsWindowController(hub: hub, onAdd: {}, onSetup: { _ in setupCalls += 1 },
            onAgents: {}, onLog: {}, onBridge: {}, onDotSetup: {}, onManageDot: { manageCalls += 1 },
            dotLinkStatus: { "Fixture Dot route" })
        controller.showWindow()
        guard let window = NSApplication.shared.windows.first(where: { $0.title == "D-Bond · Connections" }),
              let content = window.contentView else { fatalError("Connections window missing") }
        window.displayIfNeeded()
        let views = descendants(content)
        let connect = views.compactMap { $0 as? NSButton }.filter { $0.title == "Connect session…" }
        require(connect.count == 3, "Per-agent session action missing")
        let labels = views.compactMap { $0 as? NSTextField }.map(\.stringValue)
        require(labels.contains(where: { $0.contains("Direct listener offline · Dot stream not used") }), "Claude direct and Dot status conflated")
        require(labels.contains(where: { $0.contains("Direct listener offline · Dot stream offline") }), "Codex direct and Dot status conflated")
        require(labels.filter({ $0 == "Wake unverified" }).count == 3,
                "Offline agent rows falsely claim wake verification")
        for name in ["Dot", "Workflow services", "Agent sessions"] {
            require(labels.contains(name), "Missing section " + name)
        }
        let manage = views.compactMap { $0 as? NSButton }.first(where: { $0.title == "Dot settings…" })
        manage?.performClick(nil)
        require(manageCalls == 1, "Shared Dot settings did not open")
        let services = hub.snapshot().services
        func row(_ name: String) -> ServiceView { services.first(where: { $0.name == name })! }
        let workflowNames = ["Vollna / Intake", "Herald / Alerts", "Codex / HTTP producer", "Claude / Extension"]
        for name in workflowNames {
            require(ConnectionSection(row(name).role) == .workflows, "Misclassified workflow " + name)
            let warning = ConnectionDeletion.alert(for: row(name))
            require(warning.buttons.map(\.title) == ["Cancel", "Delete Workflow Connection"] && warning.informativeText.contains("update the tools"), "Missing workflow recovery warning")
        }
        require(row("My chat").role == .codexSession, "Rename lost session role")
        let sessionDelete = ConnectionDeletion.alert(for: row("My chat"))
        require(sessionDelete.buttons.map(\.title) == ["Cancel", "Delete Registration"] && sessionDelete.informativeText.contains("fresh alias"), "Missing session registration warning")
        let dotDelete = ConnectionDeletion.alert(for: row("Dot / Dispatch"))
        require(dotDelete.buttons.map(\.title) == ["Cancel", "Delete Dot Pairing"] && dotDelete.informativeText.contains("update each workflow"), "Missing Dot routing warning")
        for (serviceName, sectionName) in [("My chat", "Agent sessions"), ("Dot / Dispatch", "Dot"), ("Herald / Alerts", "Workflow services")] {
            let a11yRow = views.first { ($0.accessibilityLabel() ?? "").hasPrefix(serviceName + ", ") }
            let heading = views.compactMap { $0 as? NSTextField }.first { $0.stringValue == sectionName }!
            guard let a11yRow else { fatalError("Missing row " + serviceName) }
            require(a11yRow.superview === heading.superview && a11yRow.frame.minY >= heading.frame.maxY, "Row outside section " + sectionName)
            let nextHeading = views.compactMap { $0 as? NSTextField }.filter { ["Dot", "Workflow services", "Agent sessions"].contains($0.stringValue) && $0.frame.minY > heading.frame.minY }.map { $0.frame.minY }.min()
            require(nextHeading.map { a11yRow.frame.maxY <= $0 } ?? true, "Row overlaps next section")
        }
        require(setupCalls == 0 && hub.snapshot().eventCount == 0, "Opening session UI sent an event")
        if let qaPath = ProcessInfo.processInfo.environment["DBOND_CONNECTIONS_QA_DIR"] {
            let destination = URL(fileURLWithPath: qaPath, isDirectory: true)
            try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
            func capture(_ view: NSView, _ name: String) throws {
                view.layoutSubtreeIfNeeded()
                view.displayIfNeeded()
                guard let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { fatalError("No bitmap") }
                view.cacheDisplay(in: view.bounds, to: bitmap)
                guard let png = bitmap.representation(using: .png, properties: [:]) else { fatalError("No PNG") }
                try png.write(to: destination.appendingPathComponent(name), options: .atomic)
            }
            let keep = ["Codex / Fixture", "Claude / Fixture", "Vollna / Intake", "Dot / Dispatch"]
            for service in services where !keep.contains(service.name) { try hub.remove(id: service.id) }
            window.setContentSize(NSSize(width: 840, height: 760))
            controller.showWindow()
            window.displayIfNeeded()
            RunLoop.main.run(until: Date().addingTimeInterval(0.1))
            try capture(content, "connections.png")
            let confirmation = ConnectionDeletion.alert(for: row("Vollna / Intake"))
            confirmation.window.appearance = NSAppearance(named: .darkAqua)
            confirmation.layout()
            confirmation.window.orderFront(nil)
            confirmation.window.displayIfNeeded()
            RunLoop.main.run(until: Date().addingTimeInterval(0.2))
            guard let confirmationContent = confirmation.window.contentView else { fatalError("Missing confirmation") }
            let imagePath = destination.appendingPathComponent("workflow-deletion.png")
            let screen = Process()
            screen.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
            screen.arguments = ["-x", "-l", String(confirmation.window.windowNumber), imagePath.path]
            try screen.run()
            screen.waitUntilExit()
            if screen.terminationStatus != 0 { try capture(confirmationContent, "workflow-deletion.png") }
            confirmation.window.orderOut(nil)
            print("Fixture screenshots: " + destination.path)
        }
        print("PASS: exact private alias ID, unsafe/duplicate rejection, per-agent action, grouped rows, deletion and recovery guidance, shared connector settings, separate stream status, zero sends")
    }
}
