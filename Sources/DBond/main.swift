import AppKit
import DBondCore
import Foundation
import ServiceManagement
import Darwin

@MainActor
final class MenuController: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var currentMenu: NSMenu?
    private var hub: Hub?
    private var connectionsWindow: ConnectionsWindowController?
    private var ownerSocket: OwnerSocket?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        NSApp.applicationIconImage = Brand.app
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            if let url = Bundle.main.url(forResource: "d-bond-menu-icon", withExtension: "svg"),
               let image = NSImage(contentsOf: url), image.size.height > 0 {
                let height: CGFloat = 15
                image.size = NSSize(width: height * image.size.width / image.size.height, height: height)
                image.accessibilityDescription = "D-Bond"
                image.isTemplate = true
                button.image = image
            } else {
                button.title = "DB"
            }
        }
        statusItem.button?.toolTip = "D-Bond"
        statusItem.button?.target = self
        statusItem.button?.action = #selector(toggleMenu)
        do {
            hub = try Hub()
            if let hub { ownerSocket = try? OwnerSocket(hub:hub) }
            refreshMenu()
            if CommandLine.arguments.contains("--connections") || hub?.snapshot().services.isEmpty == true { showConnections() }
        } catch {
            setMenu([NSMenuItem(title: "D-Bond could not start: \(error.localizedDescription)", action: nil, keyEquivalent: ""),
                     NSMenuItem.separator(), quitItem()])
        }
    }

    private func openAgents() { if let hub { AgentSetup.show(hub:hub) } }

    @objc private func toggleMenu() {
        refreshMenu()
        guard let button = statusItem.button, let menu = currentMenu else { return }
        menu.popUp(positioning: nil, at: NSPoint(x: 0, y: button.bounds.minY), in: button)
    }

    private func refreshMenu() {
        guard let hub else { return }
        let snapshot = hub.snapshot()
        let menu = NSMenu()
        let title = disabled("D-Bond")
        title.image = Brand.app?.copy() as? NSImage
        title.image?.size = NSSize(width:20,height:20)
        menu.addItem(title)
        let total = snapshot.services.count
        let listeners = snapshot.services.filter { !$0.paused && ($0.subscribers + $0.directSubscribers) > 0 }.count
        menu.addItem(disabled("\(total) connections · \(listeners) listening"))
        menu.addItem(.separator())
        menu.addItem(action("Open Connections…", #selector(showConnections)))
        menu.addItem(.separator())
        let login = action("Start at Login", #selector(toggleStartAtLogin))
        login.state = LoginRegistration.isEnabled ? .on : .off
        menu.addItem(login)
        menu.addItem(.separator())
        menu.addItem(quitItem())
        currentMenu = menu
    }

    @objc private func showConnections() {
        guard let hub else {return}
        if connectionsWindow == nil {
            connectionsWindow = ConnectionsWindowController(hub:hub, onAdd:{ [weak self] in self?.pairDot() }, onSetup: { [weak self] service in self?.showServiceSetup(service) }, onAgents: { [weak self] in self?.openAgents() }, onLog: { [weak self] in self?.showError("Event history", "Open a connection’s Events to inspect its delivery history.") }, onBridge: { [weak self] in self?.checkBridgeStatus() }, onDotSetup: { [weak self] in self?.copyDotSetup() })
        }
        connectionsWindow?.showWindow()
    }


    func applicationShouldHandleReopen(_ sender:NSApplication,hasVisibleWindows flag:Bool)->Bool {
        showConnections(); return true
    }

    @objc private func pairDot() {
        let selection = Brand.alert()
        selection.messageText = "Connect a service"
        selection.informativeText = "Choose the service you want to connect. For Claude, Codex or another agent, use Agent Setup in Connections."
        selection.addButton(withTitle: "Continue")
        selection.addButton(withTitle: "Cancel")
        let preset = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 320, height: 28), pullsDown: false)
        preset.addItems(withTitles: ["Generic HTTP producer", "Herald notification", "Local demo echo"])
        selection.accessoryView = preset
        guard selection.runModal() == .alertFirstButtonReturn else { return }
        let selectedPreset = preset.indexOfSelectedItem
        let defaults: [(String, String, String)] = [
            ("Service", "Messages", "dot"),
            ("Herald", "Notifications", "herald"),
            ("Demo", "Local echo", "demo")
        ]
        let (serviceDefault, topicDefault, destinationDefault) = defaults[selectedPreset]
        let alert = Brand.alert()
        alert.messageText = "Name this connection"
        alert.informativeText = "Use the name you want to see here and in Dot, such as “Service / Messages”."
        alert.addButton(withTitle: "Create Connection")
        alert.addButton(withTitle: "Cancel")
        let field = NSTextField(string: serviceDefault)
        field.placeholderString = "Service, e.g. My service"
        field.setAccessibilityLabel("Service")
        let topic = NSTextField(string: topicDefault)
        topic.placeholderString = "Topic, e.g. Job intake"
        topic.setAccessibilityLabel("Topic")
        topic.frame = NSRect(x:0,y:0,width:280,height:24)
        field.frame = NSRect(x: 0, y: 0, width: 280, height: 24)
        let destination = NSPopUpButton(frame: NSRect(x:0,y:0,width:280,height:26),pullsDown:false)
        destination.addItems(withTitles:["Dot (native route)","Herald banner", "Local demo echo"])
        destination.selectItem(at: ["dot":0, "herald":1, "demo":2][destinationDefault] ?? 0)
        let chromeField = NSTextField(string: "")
        chromeField.placeholderString = "Chrome extension ID, if applicable (32 a–p characters)"
        chromeField.frame = NSRect(x:0,y:0,width:280,height:24)
        let accessory = NSStackView(views: [field, topic, destination, chromeField])
        accessory.orientation = .vertical
        accessory.alignment = .leading
        accessory.spacing = 10
        alert.accessoryView = accessory
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let service = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let subject = topic.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !service.isEmpty, !subject.isEmpty, !service.contains(" / ") else {
            showError("Enter a service and topic", "For example, use My service for the service and Messages for the topic.")
            return
        }
        let name = service + " / " + subject
        guard let hub else { return }
        let selected = ["dot","herald","demo"][destination.indexOfSelectedItem]
        let chromeID = chromeField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard chromeID.isEmpty || chromeID.range(of: "^[a-p]{32}$", options: .regularExpression) != nil else {
            showError("Invalid Chrome extension ID", "Enter exactly 32 letters from a through p, or leave the field blank.")
            return
        }
        let chromeOrigin = chromeID.isEmpty ? nil : "chrome-extension://" + chromeID
        DispatchQueue.global(qos: .userInitiated).async { [weak self, hub] in
            do {
                let pairing = try hub.pair(name: name, destination: selected, chromeOrigin: chromeOrigin)
                DispatchQueue.main.async {
                    self?.showPairing(pairing, name: name, preset: selectedPreset)
                    self?.refreshMenu()
                }
            } catch {
                DispatchQueue.main.async { self?.showError("Pairing failed", error.localizedDescription) }
            }
        }
    }

    private func showPairing(_ pairing: Pairing, name: String, preset: Int) {
        let example: String
        if pairing.herald {
            example = "{\"id\":\"banner-1\",\"payload\":{\"title\":\"Render complete\",\"body\":\"Your render finished successfully.\"}}"
        } else {
            example = "{\"id\":\"sample-1\",\"payload\":{\"text\":\"Hello\"}}"
        }
        let alert = Brand.alert()
        alert.messageText = "\(name) is paired locally"
        let mode: String
        switch pairing.destination {
        case "herald": mode = "Herald banners are enabled. Herald API acceptance does not prove visible display."
        case "dot": mode = "The native Dot route is selected. Queued and submitted receipts do not prove Dot acceptance or a reply; follow the later receipt states."
        default: mode = "This connection uses local demo echo only. Receipts do not mean a Dot received or answered."
        }
        let origin = pairing.chromeOrigin.map { "\nAllowed Chrome origin: \($0)" } ?? ""
        let nextStep = producerInstructions(preset: preset, destination: pairing.destination)
        alert.informativeText = "Endpoint: \(pairing.endpoint)\(origin)\n\nBearer credential (shown once):\n\(pairing.secret)\n\nPOST JSON with Content-Type: application/json and Authorization: Bearer <credential>. Example body:\n\(example)\n\nGET <endpoint>/stream?after=0 receives receipts.\n\nNext: \(nextStep)\n\n\(mode)"
        alert.alertStyle = .informational
        alert.addButton(withTitle: "Done")
        alert.addButton(withTitle: "Copy setup securely")
        alert.buttons[1].toolTip = "Copies setup text using a concealed pasteboard type; secret-aware applications may still read it."
        alert.buttons[1].keyEquivalent = ""
        let response = alert.runModal()
        if response == .alertSecondButtonReturn {
            copySetup(pairing)
        }
    }

    private func showServiceSetup(_ service: ServiceView) {
        let alert = Brand.alert()
        alert.messageText = service.name
        let preset = 0
        let status = service.paused ? "Paused." : ((service.subscribers + service.directSubscribers) > 0 ? "Paired and listening." : "Paired on this Mac.")
        let instructions = producerInstructions(preset:preset,destination:service.destination)
        alert.informativeText = "\(status)\n\n\(instructions)\n\nLocal endpoint\n\(service.endpoint)"
        alert.addButton(withTitle: "Done")
        alert.addButton(withTitle: "Copy endpoint")
        alert.addButton(withTitle: "Copy setup guide")
        alert.addButton(withTitle: "Choose icon…")
        let result = alert.runModal()
        if result.rawValue == NSApplication.ModalResponse.alertFirstButtonReturn.rawValue + 3 {
            do { try Brand.chooseIcon(for:service) } catch { showError("Could not set service icon",error.localizedDescription) }
        } else if result == .alertSecondButtonReturn {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(service.endpoint, forType: .string)
        } else if result == .alertThirdButtonReturn {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString("\(service.name)\nEndpoint: \(service.endpoint)\n\(producerInstructions(preset: preset, destination: service.destination))", forType: .string)
        }
    }



    private func producerInstructions(preset: Int, destination: String) -> String {
        return "Configure your producer with this endpoint and its one-time bearer. POST stable event IDs and read the authenticated return stream for correlated receipts."
    }

    private func copySetup(_ pairing: Pairing) {
        guard let data=try? JSONEncoder().encode(pairing), let setup=String(data:data,encoding:.utf8) else { return }
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setData(Data(setup.utf8), forType: NSPasteboard.PasteboardType("org.nspasteboard.ConcealedType"))
        pasteboard.setString(setup, forType: .string)
    }

    @objc private func copyDotSetup() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString("Keep one standing subscription to app.message.created, channel d-bond, with my existing Dot Bridge Transport. Deliver events individually and immediately. Renew automatically if supported and keep it active until I disconnect. Acknowledge each request first and reply through the bridge. Pairing establishes two-way communication for every connected service. Use the D-Bond recipient address for an addressed message and the return address for a question to its sender. Keep payloads out of ordinary chat. Do not use Slack or unsubscribe after answering.",forType:.string)
    }
    @objc private func checkBridgeStatus() {
        guard let hub else { return }
        DispatchQueue.global(qos: .utility).async { [weak self, hub] in
            let value = hub.bridgeStatus()
            DispatchQueue.main.async { self?.showError("Bridge status", value) }
        }
    }

    @objc private func toggleStartAtLogin() {
        do {
            try LoginRegistration.setEnabled(!LoginRegistration.isEnabled)
            refreshMenu()
            if !LoginRegistration.usesSupervisor && SMAppService.mainApp.status == .requiresApproval {
                showError("Start at Login needs approval", "macOS requires approval in System Settings → General → Login Items. D-Bond is not yet enabled to start at login.")
            }
        } catch {
            refreshMenu()
            showError("Could not change Start at Login", error.localizedDescription)
        }
    }

    private func showError(_ title: String, _ body: String) {
        let alert = Brand.alert()
        alert.messageText = title
        alert.informativeText = body
        alert.runModal()
    }

    private func disabled(_ title: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
    }

    private func action(_ title: String, _ selector: Selector) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: selector, keyEquivalent: "")
        item.target = self
        return item
    }

    private func quitItem() -> NSMenuItem {
        let item = NSMenuItem(title: "Quit D-Bond", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.target = NSApp
        return item
    }

    private func setMenu(_ items: [NSMenuItem]) {
        let menu = NSMenu()
        items.forEach(menu.addItem)
        currentMenu = menu
    }
}

MainActor.assumeIsolated {
    let app = NSApplication.shared
    let delegate = MenuController()
    app.delegate = delegate
    withExtendedLifetime(delegate) { app.run() }
}
