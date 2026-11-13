import AppKit
import ServiceManagement

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

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 432, height: 220),
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

        let content = NSStackView(views: [header, divider, preferences])
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
