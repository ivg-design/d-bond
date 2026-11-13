import AppKit
import DBondCore
import Foundation

@MainActor
private func require(_ condition: @autoclosure () -> Bool, _ detail: String) {
    guard condition() else { fatalError(detail) }
}

@MainActor
private func firstScroll(in view: NSView) -> NSScrollView? {
    if let scroll = view as? NSScrollView { return scroll }
    for child in view.subviews {
        if let found = firstScroll(in: child) { return found }
    }
    return nil
}

@MainActor
private func find(_ id: String, in view: NSView) -> NSView? {
    if view.identifier?.rawValue == id { return view }
    for child in view.subviews {
        if let found = find(id, in: child) { return found }
    }
    return nil
}

@main
struct AgentSetupFixture {
    @MainActor static func main() throws {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let directory = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        let installers = directory.deletingLastPathComponent().appendingPathComponent("agent-installers")
        let code = installers.appendingPathComponent("claude-code-marketplace/plugin/.claude-plugin")
        try FileManager.default.createDirectory(at: code, withIntermediateDirectories: true)
        try Data(#"{"version":"0.1.10"}"#.utf8).write(to: code.appendingPathComponent("plugin.json"))
        try Data(#"{"version":"0.1.8"}"#.utf8).write(to: installers.appendingPathComponent("claude-desktop-manifest.json"))
        require(AgentSetup.availableDesktopUpdate(installed: "0.1.7", installers: installers) == "0.1.8",
                "Desktop must show its own newer package")
        require(AgentSetup.availableDesktopUpdate(installed: "0.1.8", installers: installers) == nil,
                "A current Desktop extension must stay current when the Code plugin is newer")
        let hub = try Hub(directory: directory, port: 0)
        var first: NSWindow?
        for iteration in 0..<4 {
            AgentSetup.show(hub: hub)
            guard let window = app.windows.first(where: { $0.title.hasSuffix("Agent Setup") }) else {
                fatalError("Agent Setup window missing at iteration \(iteration)")
            }
            require(window.isVisible, "Agent Setup was not visible after open")
            require(!window.isReleasedWhenClosed, "Agent Setup closes by releasing its cached window")
            if let first { require(window === first, "Agent Setup created another cached window") }
            else { first = window }
            guard let content = window.contentView, let scroll = firstScroll(in: content) else {
                fatalError("Agent Setup scroll missing")
            }
            scroll.contentView.scroll(to: NSPoint(x: 0, y: 100))
            scroll.reflectScrolledClipView(scroll.contentView)
            window.close()
            require(!window.isVisible, "Agent Setup did not close")
            require(hub.snapshot().eventCount == 0, "Opening Agent Setup sent an event")
        }
        AgentSetup.show(hub: hub)
        guard let window = first, let content = window.contentView, let scroll = firstScroll(in: content) else {
            fatalError("Final reopen failed")
        }
        require(window.isVisible, "Final reopen not visible")
        require(scroll.contentView.bounds.origin.y == 0, "Reopen did not reset scroll")
        guard let codex = find("setup.codex.status", in: content) as? NSTextField,
              let claude = find("setup.claude.status", in: content) as? NSTextField,
              let desktop = find("setup.desktop.status", in: content) as? NSTextField,
              let refresh = find("setup.refresh", in: content) as? NSButton else {
            fatalError("Read-only setup status controls missing")
        }
        let deadline = Date().addingTimeInterval(10)
        while !refresh.isEnabled && Date() < deadline {
            RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        }
        require(refresh.isEnabled, "Read-only installation check did not finish")
        require(!codex.stringValue.isEmpty && !claude.stringValue.isEmpty && !desktop.stringValue.isEmpty,
                "Installation status labels did not update")
        require(hub.snapshot().eventCount == 0, "Reopen sent an event")
        print("PASS: Agent Setup 5 native open/close cycles, retained window, scroll reset, read-only status labels, zero sends")
    }
}
