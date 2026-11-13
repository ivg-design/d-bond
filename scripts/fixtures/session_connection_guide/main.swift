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
        var setupCalls = 0
        let controller = ConnectionsWindowController(hub: hub, onAdd: {}, onSetup: { _ in setupCalls += 1 },
            onAgents: {}, onLog: {}, onBridge: {}, onDotSetup: {})
        controller.showWindow()
        guard let window = NSApplication.shared.windows.first(where: { $0.title == "D-Bond · Connections" }),
              let content = window.contentView else { fatalError("Connections window missing") }
        window.displayIfNeeded()
        let views = descendants(content)
        let connect = views.compactMap { $0 as? NSButton }.filter { $0.title == "Connect session…" }
        require(connect.count == 2, "Per-agent session action missing")
        let labels = views.compactMap { $0 as? NSTextField }.map(\.stringValue)
        require(labels.contains(where: { $0.contains("Direct listener offline · Dot stream not used") }), "Claude direct and Dot status conflated")
        require(labels.contains(where: { $0.contains("Direct listener offline · Dot stream offline") }), "Codex direct and Dot status conflated")
        require(labels.filter({ $0 == "Wake unverified" }).count == 2,
                "Offline agent rows falsely claim wake verification")
        require(setupCalls == 0 && hub.snapshot().eventCount == 0, "Opening session UI sent an event")
        print("PASS: exact private alias ID, unsafe/duplicate rejection, per-agent action, separate stream status, zero sends")
    }
}
