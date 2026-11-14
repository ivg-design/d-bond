import AppKit

/// Optional public HTTPS setup. No personal domain, token or URL is bundled.
@MainActor
final class DotIngressWindowController: NSWindowController {
    static let shared = DotIngressWindowController()
    private let controller = DotIngressController.shared
    private let mode = NSPopUpButton()
    private let host = NSTextField(string: "")
    private let token = NSSecureTextField(string: "")
    private let instructions = NSTextField(wrappingLabelWithString: "")
    private let status = NSTextField(wrappingLabelWithString: "")
    private let endpoint = NSTextField(labelWithString: "")
    private let connect = NSButton(title: "Start Ingress", target: nil, action: nil)
    private let stop = NSButton(title: "Stop", target: nil, action: nil)
    private let verify = NSButton(title: "Verify HTTPS", target: nil, action: nil)
    private var observer: NSObjectProtocol?

    private init() {
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 580, height: 570),
                              styleMask: [.titled, .closable], backing: .buffered, defer: false)
        super.init(window: window)
        window.title = "Public HTTPS for Dot"
        window.identifier = .init("dbond.dotIngress.window")
        window.isReleasedWhenClosed = false
        window.center()
        let title = NSTextField(labelWithString: "Make your Dot connector reachable")
        title.font = .systemFont(ofSize: 18, weight: .semibold)
        let introduction = NSTextField(wrappingLabelWithString:
            "Dot needs a stable public HTTPS address. D-Bond can run a named Cloudflare Tunnel, or use your existing HTTPS service. Local access alone cannot connect Dot.")
        mode.addItems(withTitles: ["Cloudflare Tunnel · account and domain required", "My existing HTTPS service"])
        mode.target = self; mode.action = #selector(selectIngressMode)
        mode.identifier = .init("dotIngress.mode")
        host.placeholderString = "Your public DNS hostname"
        host.identifier = .init("dotIngress.hostname")
        token.placeholderString = "Tunnel token · saved privately in login Keychain"
        token.identifier = .init("dotIngress.token")
        let origin = NSTextField(labelWithString: "http://127.0.0.1:18770")
        origin.font = .monospacedSystemFont(ofSize: 13, weight: .medium)
        origin.isSelectable = true
        let originTitle = NSTextField(labelWithString: "Local service for your public route")
        originTitle.font = .systemFont(ofSize: 12, weight: .semibold)
        let copy = NSButton(title: "Copy Service", target: self, action: #selector(copyOrigin))
        let originRow = NSStackView(views: [origin, copy]); originRow.spacing = 12
        let dashboard = NSButton(title: "Open Cloudflare Tunnels…", target: self, action: #selector(openDashboard))
        endpoint.isSelectable = true
        endpoint.textColor = .secondaryLabelColor
        status.textColor = .secondaryLabelColor
        status.identifier = .init("dotIngress.status")
        instructions.textColor = .secondaryLabelColor
        connect.target = self; connect.action = #selector(start)
        stop.target = self; stop.action = #selector(stopIngress)
        verify.target = self; verify.action = #selector(verifyIngress)
        let buttons = NSStackView(views: [connect, verify, stop]); buttons.spacing = 10
        let boundary = NSTextField(wrappingLabelWithString:
            "This restricted route exposes only the MCP connector and its OAuth consent/token endpoints. D-Bond’s application API and local owner controls stay private. Keep D-Bond running for this route to remain available.")
        boundary.font = .systemFont(ofSize: 11)
        boundary.textColor = .secondaryLabelColor
        let content = NSStackView(views: [title, introduction, mode,
            NSTextField(labelWithString: "Public hostname"), host, token,
            instructions, dashboard, originTitle, originRow, endpoint, buttons, status, boundary])
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 12
        guard let root = window.contentView else { return }
        root.addSubview(content)
        content.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 24),
            content.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -24),
            content.topAnchor.constraint(equalTo: root.topAnchor, constant: 24),
            content.bottomAnchor.constraint(lessThanOrEqualTo: root.bottomAnchor, constant: -22)
        ])
        for field in [introduction, host, token, instructions, status, boundary] {
            field.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true
        }
        mode.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true
        observer = NotificationCenter.default.addObserver(forName: DotIngressController.changed, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
    }

    required init?(coder: NSCoder) { nil }

    func present() {
        mode.selectItem(at: controller.mode == "cloudflare" ? 0 : 1)
        host.stringValue = controller.hostname
        token.stringValue = ""
        selectIngressMode()
        refresh()
        NSApp.activate(ignoringOtherApps: true)
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
    }

    @objc private func selectIngressMode() {
        let cloud = mode.indexOfSelectedItem == 0
        token.isHidden = !cloud
        instructions.stringValue = cloud
            ? "In Cloudflare, create a named tunnel and add your hostname as a published application. Set its service to the local address below. Paste its tunnel token here; D-Bond downloads the official helper and runs it. No Terminal command is needed."
            : "Configure your HTTPS reverse proxy to forward this hostname to the local address below. Start ingress here, then verify HTTPS. Cloudflare and a tunnel token are not required."
    }

    private func refresh() {
        status.stringValue = controller.state
        connect.isEnabled = !controller.running
        stop.isEnabled = controller.running
        verify.isEnabled = controller.running
        host.isEnabled = !controller.running
        token.isEnabled = !controller.running
        mode.isEnabled = !controller.running
        endpoint.stringValue = controller.publicOrigin.isEmpty ? "" : controller.publicOrigin + "/mcp"
    }

    @objc private func start() {
        do {
            try controller.start(mode: mode.indexOfSelectedItem == 0 ? "cloudflare" : "https",
                                 hostname: host.stringValue, token: token.stringValue)
            token.stringValue = ""
        } catch {
            let alert = Brand.alert()
            alert.messageText = "Ingress could not start"
            alert.informativeText = error.localizedDescription
            if let window { alert.beginSheetModal(for: window) }
        }
        refresh()
    }
    @objc private func stopIngress() { controller.stop(); refresh() }
    @objc private func verifyIngress() { controller.verifyHTTPS() }
    @objc private func copyOrigin() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString("http://127.0.0.1:18770", forType: .string)
    }
    @objc private func openDashboard() {
        NSWorkspace.shared.open(URL(string: "https://dash.cloudflare.com/?to=/:account/networks/tunnels")!)
    }
}
