import AppKit
import DBondCore
import Foundation
import ServiceManagement

@MainActor
final class MenuController: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var currentMenu: NSMenu?
    private var hub: Hub?
    private var connectionsWindow: ConnectionsWindowController?
    private var ownerSocket: OwnerSocket?
    private var dotSummary = "Checking Dot’s standing link…"

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let button = statusItem.button {
            if let image = NSImage(systemSymbolName: "point.3.connected.trianglepath.dotted", accessibilityDescription: "D-Bond") {
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

    @objc private func toggleMenu() {
        refreshMenu()
        guard let button = statusItem.button, let menu = currentMenu else { return }
        menu.popUp(positioning: nil, at: NSPoint(x: 0, y: button.bounds.minY), in: button)
    }

    private func refreshMenu() {
        guard let hub else { return }
        let snapshot = hub.snapshot()
        let menu = NSMenu()
        menu.addItem(disabled("D-Bond · local bridge"))
        let dotStatus = disabled(dotSummary)
        menu.addItem(dotStatus)
        Task { [weak self, weak dotStatus] in
            let result = await Task.detached(priority:.utility) { hub.bridgeStatus() }.value
            self?.dotSummary = result; dotStatus?.title = result
        }
        menu.addItem(action("Copy one-time Dot setup", #selector(copyDotSetup)))
        menu.addItem(.separator())
        menu.addItem(action("Connections…", #selector(showConnections)))
        menu.addItem(action("Connect a service…", #selector(pairDot)))
        menu.addItem(action("Copy reusable HTTP example", #selector(copyExample)))
        menu.addItem(.separator())
        menu.addItem(disabled("Paired services (\(snapshot.services.count))"))
        if snapshot.services.isEmpty {
            menu.addItem(disabled("No services paired yet"))
        } else {
            for service in snapshot.services {
                let state = service.paused ? "Paused" : (service.subscribers > 0 ? "Connected" : "Paired")
                let row=NSMenuItem(title:"\(service.name) · \(state)",action:nil,keyEquivalent:"")
                let details=NSMenu()
                let mode=["dot":"Dot","herald":"Herald notifications","demo":"Local echo"][service.destination] ?? service.destination
                details.addItem(disabled("Destination: "+mode))
                details.addItem(disabled(service.subscribers == 0 ? "No open return stream" : "\(service.subscribers) open return stream(s)"))
                if let seen=service.lastSeen { details.addItem(disabled("Last incoming event: "+seen)) }
                if let last=service.lastState { details.addItem(disabled(String(friendlyState(last,herald:service.herald).dropFirst(3)))) }
                if service.queued+service.inFlight>0 {details.addItem(disabled("\(service.queued) queued · \(service.inFlight) dispatching"))}
                if let origin=service.chromeOrigin {details.addItem(disabled(origin))}
                details.addItem(.separator())
                if service.destination != "demo" {
                    let address=action("Copy address for Dot", #selector(copyDotAddress(_:)))
                    address.representedObject=service.id; details.addItem(address)
                }
                let toggle=action(service.paused ? "Resume" : "Pause",#selector(togglePairing(_:)))
                toggle.representedObject=service.id;details.addItem(toggle)
                let remove=action("Delete pairing…",#selector(removePairing(_:)))
                remove.representedObject=service.id;details.addItem(remove)
                row.submenu=details;menu.addItem(row)
            }
        }
        menu.addItem(.separator())
        menu.addItem(action("Check bridge status…", #selector(checkBridgeStatus)))
        menu.addItem(action("Check Herald status…", #selector(checkHeraldStatus)))
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
            connectionsWindow = ConnectionsWindowController(hub:hub, onAdd:{ [weak self] in self?.pairDot() }, onSetup: { [weak self] service in self?.showServiceSetup(service) })
        }
        connectionsWindow?.showWindow()
    }
    func applicationShouldHandleReopen(_ sender:NSApplication,hasVisibleWindows flag:Bool)->Bool {
        showConnections(); return true
    }

    @objc private func pairDot() {
        let selection = NSAlert()
        selection.messageText = "Connect a service"
        selection.informativeText = "Choose a starting point. This creates a local connection only; configure the sending service separately. Existing connections continue working."
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
        let alert = NSAlert()
        alert.messageText = "Name this connection"
        alert.informativeText = "The service and topic form its visible name. Select where events go. A Chrome origin is needed only for a browser extension that calls this endpoint directly."
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
        let alert = NSAlert()
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
        let alert = NSAlert()
        alert.messageText = "Setup · \(service.name)"
        let preset = 0
        alert.informativeText = "Local endpoint:\n\(service.endpoint)\n\n\(producerInstructions(preset: preset, destination: service.destination))\n\nThe bearer credential was shown only when this connection was created. If it was lost, delete this pairing and create a new one, then update the producer. Pairing and local receipt stages do not activate or verify the producer."
        alert.addButton(withTitle: "Done")
        alert.addButton(withTitle: "Copy endpoint")
        alert.addButton(withTitle: "Copy setup guide")
        let result = alert.runModal()
        if result == .alertSecondButtonReturn {
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
    @objc private func copyDotAddress(_ sender:NSMenuItem) {
        guard let id=sender.representedObject as? String,
              let connection=hub?.snapshot().services.first(where: { $0.id == id }) else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(connection.name + "\nUse send_to_app on channel d-bond. Set metadata.recipient_caller_id to d-bond and metadata.recipient_conversation_id to "+id+". Use a stable idempotency_key for this question.",forType:.string)
    }

    @objc private func copyExample() {
        guard let first = hub?.snapshot().services.first else {
            showError("No service connected", "Connect a webhook service first.")
            return
        }
        let pairing = first
        let body = pairing.herald
            ? "{\"id\":\"banner-1\",\"payload\":{\"title\":\"Render complete\",\"body\":\"Your render finished successfully.\"}}"
            : "{\"id\":\"sample-1\",\"payload\":{\"text\":\"Hello\"}}"
        let example = "curl -X POST '<service-endpoint>' -H 'Content-Type: application/json' -H 'Authorization: Bearer <service-credential>' -d '\(body)'\n\nGET '<service-endpoint>/stream?after=0' for receipts. Replace placeholders locally. Service: \(pairing.name)."
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(example, forType: .string)
        showError("HTTP example copied", "The example uses placeholders so it does not expose the service credential. Destination: \(pairing.destination).")
    }

    @objc private func removePairing(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String, let hub else { return }
        let alert = NSAlert()
        alert.messageText = "Remove this service connection?"
        alert.informativeText = "This revokes the local bearer credential and deletes queued events and receipts. A delivery already dispatching may still finish."
        alert.addButton(withTitle: "Remove")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        do { try hub.remove(id: id); refreshMenu() }
        catch { showError("Could not remove pairing", error.localizedDescription) }
    }

    @objc private func togglePairing(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String, let hub,
              let service = hub.snapshot().services.first(where: { $0.id == id }) else { return }
        do { try hub.setPaused(id: id, paused: !service.paused); refreshMenu() }
        catch { showError("Could not update service connection", error.localizedDescription) }
    }

    @objc private func checkBridgeStatus() {
        guard let hub else { return }
        DispatchQueue.global(qos: .utility).async { [weak self, hub] in
            let value = hub.bridgeStatus() + "\n\n" + hub.dotLinkStatus()
            DispatchQueue.main.async { self?.showError("Bridge status", value) }
        }
    }

    @objc private func checkHeraldStatus() {
        guard let hub else { return }
        DispatchQueue.global(qos: .utility).async { [weak self, hub] in
            let value = hub.heraldStatus()
            DispatchQueue.main.async { self?.showError("Herald status", value) }
        }
    }

    @objc private func toggleStartAtLogin() {
        do {
            try LoginRegistration.setEnabled(!LoginRegistration.isEnabled)
            refreshMenu()
            if SMAppService.mainApp.status == .requiresApproval {
                showError("Start at Login needs approval", "macOS requires approval in System Settings → General → Login Items. D-Bond is not yet enabled to start at login.")
            }
        } catch {
            refreshMenu()
            showError("Could not change Start at Login", error.localizedDescription)
        }
    }

    private func showError(_ title: String, _ body: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = body
        alert.runModal()
    }

    private func friendlyState(_ state: String, herald: Bool) -> String {
        if herald {
            switch state {
            case "herald_accepted": return " · Herald accepted (display unverified)"
            case "herald_unconfirmed": return " · Herald outcome unconfirmed; check History"
            case "sending_banner": return " · sending to Herald"
            default: return " · \(state)"
            }
        }
        switch state {
        case "dot_acknowledged": return " · Dot started"
        case "dot_replied": return " · Dot replied"
        case "dot_waiting_for_link": return " · stored, waiting for Dot link"
        case "dot_stored": return " · stored, awaiting Dot"
        case "dot_push": return " · message from Dot"
        case "dot_unconfirmed": return " · submission unconfirmed"
        case "dot_route_blocked": return " · routing blocked"
        case "local_reply": return " · local demo receipt"
        case "queued": return " · received locally"
        case "dispatching": return " · local adapter dispatching"
        case "outcome_unknown": return " · dispatch outcome unknown; check destination"
        case "adapter_failed": return " · demo adapter failed"
        default: return " · \(state)"
        }
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
