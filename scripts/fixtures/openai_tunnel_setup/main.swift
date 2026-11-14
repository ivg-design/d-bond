import AppKit
import DBondCore

@main
struct PrivateDotFixture {
    @MainActor static func main() throws {
        NSApplication.shared.setActivationPolicy(.accessory)
        if let source = ProcessInfo.processInfo.environment["DBOND_FIXTURE_ICON"] { NSApp.applicationIconImage = NSImage(contentsOfFile: source) }
        let root = URL(fileURLWithPath: CommandLine.arguments[1])
        let suite = "dbond-tunnel-ui-" + UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        defaults.set("tunnel_fixtureA", forKey: "openAITunnelID")
        let command = root.appendingPathComponent("host-fixture")
        try """
        #!/bin/sh
        cat "$2/status.json"
        """.write(to: command, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: command.path)
        let record = root.appendingPathComponent("status.json")
        func status(_ tunnel: Bool, _ subscribed: Bool = true, servedID: String = "tunnel_fixtureA") throws {
            let row: [String: Any] = ["configured": true, "running": true, "subscribed": subscribed, "tunnel_configured": true, "tunnel_id": "tunnel_fixtureA",
                "identity": ["tunnel_id": servedID, "connected": subscribed, "active_subscriptions": subscribed ? 1 : 0, "tunnel_listening": true, "tunnel_subscribed": tunnel] as [String: Any]]
            try JSONSerialization.data(withJSONObject: row).write(to: record)
        }
        let connector = DotConnector(executable: command, stateDirectory: root, routeURL: root.appendingPathComponent("route.json"), defaults: defaults)
        let runtime = OpenAITunnelRuntime(defaults: defaults, stateDirectory: root.appendingPathComponent("runtime"), connectorStateDirectory: root, executable: command)
        var cutovers = 0
        let controller = OpenAITunnelSetupWindow(connector: connector, runtime: runtime) { cutovers += 1 }
        func spin() {
            let deadline = Date().addingTimeInterval(0.35)
            while Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
        }
        try status(false)
        controller.present()
        spin()
        guard let window = controller.window, let scroll = window.contentView as? NSScrollView, let document = scroll.documentView else { fatalError("Private tunnel wizard missing") }
        func children(_ item: NSView) -> [NSView] { [item] + item.subviews.flatMap(children) }
        let views = children(document)
        func view<T: NSView>(_ id: String) -> T { views.first(where: { $0.identifier?.rawValue == id }) as! T }
        let subscription: NSTextField = view("openaiTunnel.subscription")
        let select: NSButton = view("openaiTunnel.select")
        let key: NSSecureTextField = view("openaiTunnel.key")
        let refresh = views.compactMap { $0 as? NSButton }.first(where: { $0.title == "Check connection" })!
        precondition(subscription.stringValue.contains("replace that callback"))
        precondition(!select.isEnabled && key.stringValue.isEmpty)
        try status(true)
        refresh.performClick(nil)
        spin()
        precondition(subscription.stringValue.contains("subscription is verified"))
        precondition(!select.isEnabled, "A callback alone must not claim the local tunnel client is ready")
        try status(true, servedID: "tunnel_fixtureB")
        refresh.performClick(nil)
        spin()
        precondition(subscription.stringValue.contains("replace that callback") && !select.isEnabled)
        try status(false, false)
        refresh.performClick(nil)
        spin()
        precondition(subscription.stringValue.contains("Waiting for Dot"))
        precondition(cutovers == 0 && !FileManager.default.fileExists(atPath: root.appendingPathComponent("route.json").path))
        window.setContentSize(NSSize(width: 550, height: 420))
        window.displayIfNeeded()
        document.layoutSubtreeIfNeeded()
        precondition(document.bounds.width > 450 && document.bounds.height > 480, "Wizard must scroll at small window sizes")
        let last = select.convert(select.bounds, to: document)
        precondition(last.minY >= 0 && last.maxY <= document.bounds.maxY && last.maxX <= document.bounds.maxX)
        scroll.documentView?.scrollToVisible(last)
        precondition(scroll.documentVisibleRect.intersects(last), "Workflow controls must be reachable")
        if let path = ProcessInfo.processInfo.environment["DBOND_TUNNEL_UI_IMAGE"] {
            window.setContentSize(NSSize(width: 625, height: 646)); window.displayIfNeeded()
            document.scroll(.zero)
            let capture = Process()
            capture.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
            capture.arguments = ["-x", "-l", String(window.windowNumber), path]
            try capture.run(); capture.waitUntilExit()
        }
        window.close()
        print("PASS: private tunnel settings render, distinct HTTPS/tunnel/absent subscription state, offline cutover disabled and controls scroll within bounds.")
    }
}
