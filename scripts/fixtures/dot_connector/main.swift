import AppKit
import DBondCore

@MainActor
func runFixture() {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    let root = FileManager.default.temporaryDirectory.appendingPathComponent("dbond-settings-" + UUID().uuidString)
    try! FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: root) }
    let bundled = Bundle.main.bundleURL.pathExtension == "app"
    let executable = bundled ? Bundle.main.resourceURL!.appendingPathComponent("dot-host/connector") : root.appendingPathComponent("connector")
    try! FileManager.default.createDirectory(at: executable.deletingLastPathComponent(), withIntermediateDirectories: true)
    try! """
    #!/bin/sh
    printf '%s\\n' '{"configured":true,"running":true,"subscribed":false,"public_url":"https://connector.example.com","redirect_uri":"https://chatgpt.com/callback","mcp_url":"https://connector.example.com/mcp","identity":{"connected":false,"active_subscriptions":0}}'
    """.write(to: executable, atomically: true, encoding: .utf8)
    try! FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: executable.path)
    let suite = "dbond-settings-" + UUID().uuidString
    let defaults = UserDefaults(suiteName: suite)!
    defer { defaults.removePersistentDomain(forName: suite) }
    let connector: DotConnector
    if bundled {
        precondition(DotConnector.defaultExecutable == executable, "Relocated app must resolve its own bundled helper")
        connector = DotConnector(stateDirectory: root, routeURL: root.appendingPathComponent("route.json"), defaults: defaults)
    } else {
        connector = DotConnector(executable: executable, stateDirectory: root, routeURL: root.appendingPathComponent("route.json"), defaults: defaults)
    }
    let controller = SettingsWindowController(defaults: defaults, connector: connector)
    controller.presentDotConnector()
    let deadline = Date().addingTimeInterval(0.7)
    while Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.02)) }
    guard let window = controller.window, let content = window.contentView else { fatalError("Missing Settings window") }
    func descendants(_ view: NSView) -> [NSView] { [view] + view.subviews.flatMap(descendants) }
    let views = descendants(content)
    guard let status = views.first(where: { $0.identifier?.rawValue == "settings.dotConnector.status" }) as? NSTextField,
          let use = views.first(where: { $0.identifier?.rawValue == "settings.dotConnector.useOwned" }) as? NSButton else { fatalError("Missing connector controls") }
    precondition(status.stringValue.contains("Legacy (unchanged)"))
    precondition(status.stringValue.contains("Dot not connected"))
    precondition(!use.isEnabled, "Unsubscribed route must stay unavailable")
    precondition(!FileManager.default.fileExists(atPath: root.appendingPathComponent("route.json").path))
    content.layoutSubtreeIfNeeded()
    for view in views where !view.isHidden {
        let frame = view.convert(view.bounds, to: content)
        precondition(frame.maxY <= content.bounds.maxY + 1 && frame.minY >= -1, "Control outside Settings window")
    }
    let destination = ProcessInfo.processInfo.environment["DOT_CONNECTOR_QA_DIR"].map(URL.init(fileURLWithPath:)) ?? root
    try! FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
    if let bitmap = content.bitmapImageRepForCachingDisplay(in: content.bounds) {
        content.cacheDisplay(in: content.bounds, to: bitmap)
        try! bitmap.representation(using: .png, properties: [:])!.write(to: destination.appendingPathComponent("dot-connector-settings.png"))
    }
    let capture = Process()
    capture.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    capture.arguments = ["-x", "-l", String(window.windowNumber), destination.appendingPathComponent("dot-connector-window.png").path]
    capture.standardError = FileHandle.nullDevice
    try? capture.run()
    capture.waitUntilExit()
    window.close()
    print("Dot connector Settings fixture passed: relocated app helper resolution, safe pending identity, gated selection, controls contained.")
}
@main struct Fixture { @MainActor static func main() { runFixture() } }
