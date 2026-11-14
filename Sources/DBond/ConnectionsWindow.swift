import AppKit
import DBondCore

/// A reusable connections window. Its one-second refresh reads only Hub's local snapshot.
@MainActor
final class ConnectionsWindowController: NSObject, NSWindowDelegate {
    private let hub: Hub
    private let onAdd: () -> Void
    private let onAgents, onLog, onBridge, onDotSetup, onManageDot: () -> Void
    private let onSetup: (ServiceView) -> Void
    private let dotLinkStatus: @Sendable () -> String
    private var checkingDot = false
    private var window: NSWindow?
    private var rootView: ConnectionsRootView?
    private var documentView: ConnectionDocumentView?
    private var rows: [String: ConnectionRowView] = [:]
    private var refreshTimer: Timer?
    private var ruleEditors: [String: RuleEditor] = [:]

    init(hub: Hub, onAdd: @escaping () -> Void, onSetup: @escaping (ServiceView) -> Void, onAgents: @escaping () -> Void, onLog: @escaping () -> Void, onBridge: @escaping () -> Void, onDotSetup: @escaping () -> Void, onManageDot: @escaping () -> Void, dotLinkStatus: (@Sendable () -> String)? = nil) {
        self.dotLinkStatus = dotLinkStatus ?? { hub.dotLinkStatus() }
        self.hub = hub
        self.onAdd = onAdd
        self.onAgents = onAgents; self.onLog = onLog; self.onBridge = onBridge; self.onDotSetup = onDotSetup
        self.onManageDot = onManageDot
        self.onSetup = onSetup
        super.init()
    }

    func showWindow() {
        if window == nil { buildWindow() }
        refresh()
        guard let window else { return }
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        refreshTimer?.invalidate()
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.refresh() }
        }
    }

    func windowDidBecomeKey(_ notification: Notification) {
        guard !checkingDot else { return }
        checkingDot = true
        let check = dotLinkStatus
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let status = check()
            DispatchQueue.main.async { [weak self] in
                self?.documentView?.setDotStatus(status)
                self?.checkingDot = false
            }
        }
    }

    func windowWillClose(_ notification: Notification) {
        refreshTimer?.invalidate()
        refreshTimer = nil
    }

    private func buildWindow() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 820, height: 610),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "D-Bond · Connections"
        window.minSize = NSSize(width: 650, height: 390)
        window.isReleasedWhenClosed = false
        window.appearance = NSAppearance(named: .darkAqua)
        window.delegate = self

        let title = NSTextField(labelWithString: "Connections")
        title.font = .systemFont(ofSize: 23, weight: .semibold)
        let subtitle = NSTextField(labelWithString: "Services and agent sessions paired with this Mac.")
        subtitle.font = .systemFont(ofSize: 12)
        subtitle.textColor = .secondaryLabelColor
        let add = NSButton(title: "Add Connection…", target: self, action: #selector(addConnection))
        add.bezelStyle = .rounded
        add.controlSize = .regular
        add.image = NSImage(systemSymbolName:"plus",accessibilityDescription:nil)
        add.imagePosition = .imageLeading

        let scroll = NSScrollView()
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = true
        scroll.hasHorizontalScroller = false
        scroll.autohidesScrollers = true
        scroll.borderType = .noBorder
        let document = ConnectionDocumentView(onManageDot: onManageDot)
        scroll.documentView = document
        self.documentView = document

        let agents = NSButton(title: "Agent Setup…", target: self, action: #selector(openAgents))
        agents.bezelStyle = .rounded
        agents.image = NSImage(systemSymbolName:"macwindow.badge.plus",accessibilityDescription:nil)
        agents.imagePosition = .imageLeading
        let more = NSPopUpButton(frame:.zero,pullsDown:true)
        more.bezelStyle = .rounded
        more.addItem(withTitle:"Bridge Tools")
        for (name, action) in [("Webhook Log…", #selector(openLog)), ("Connection Status…", #selector(openBridge)), ("Copy Dot Setup", #selector(copySetup))] {
            let item = NSMenuItem(title:name,action:action,keyEquivalent:""); item.target=self; more.menu?.addItem(item)
        }
        let root = ConnectionsRootView(title: title, subtitle: subtitle, addButton: add, agents:agents, more:more, scrollView: scroll)
        root.wantsLayer = true
        root.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        self.rootView = root
        window.contentView = root
        self.window = window
    }

    private func refresh() {
        guard let documentView, let rootView else { return }
        let services = hub.snapshot().services.sorted {
            $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending
        }
        let liveIDs = Set(services.map(\.id))
        for id in rows.keys.filter({ !liveIDs.contains($0) }) {
            rows.removeValue(forKey: id)?.removeFromSuperview()
        }
        var sections: [ConnectionSection: [ConnectionRowView]] = [:]
        for service in services {
            let row: ConnectionRowView
            if let existing = rows[service.id] {
                row = existing
            } else {
                row = ConnectionRowView(
                    service: service,
                    onPause: { [weak self] id in self?.togglePause(id) },
                    onDelete: { [weak self] id in self?.confirmDelete(id) },
                    onSetup: { [weak self] id in self?.showSetup(id) },
                    onSOP: { [weak self] id in self?.editSOP(id) },
                    onTrace: { [weak self] id in self?.showTrace(id) }
                )
                rows[service.id] = row
            }
            row.update(service)
            sections[ConnectionSection(service.role), default: []].append(row)
        }
        documentView.setSections(sections)
        rootView.setCounts(services.count, listening: services.filter { !$0.paused && ($0.subscribers + $0.directSubscribers > 0) }.count)
    }

    @objc private func openAgents() { onAgents() }
    @objc private func openLog() { onLog() }
    @objc private func openBridge() { onBridge() }
    @objc private func copySetup() { onDotSetup() }

    @objc private func addConnection() {
        onAdd()
        refresh()
    }

    private func togglePause(_ id: String) {
        guard let service = hub.snapshot().services.first(where: { $0.id == id }) else { return }
        do {
            try hub.setPaused(id: id, paused: !service.paused)
            refresh()
        } catch {
            showError("Could not update connection", error.localizedDescription)
        }
    }

    private func showSetup(_ id: String) {
        guard let service = hub.snapshot().services.first(where: { $0.id == id }) else { return }
        guard let kind = AgentSessionKind(service: service) else { onSetup(service); return }
        if !service.paused && service.directSubscribers > 0 && service.wakeState != "verified" {
            do { _ = try hub.beginWakeTest(service: id) }
            catch { showError("Could not test this session", error.localizedDescription); return }
            refresh()
        }
        let current = hub.snapshot().services.first(where: { $0.id == id }) ?? service
        SessionConnectionGuide.show(current, kind: kind)
    }

    private func editSOP(_ id: String) {
        if let editor = ruleEditors[id] { editor.show(); return }
        guard let service = hub.snapshot().services.first(where: { $0.id == id }) else { return }
        let editor = RuleEditor(serviceName: service.name, initial: service.sop,
            load: { [hub] in try hub.getSOP(service: id) },
            persist: { [weak self, hub] next, version in
                try hub.setSOP(service: id, sop: next, expectedVersion: version)
                self?.refresh()
            })
        ruleEditors[id] = editor
        editor.onClose = { [weak self] in self?.ruleEditors.removeValue(forKey: id) }
        editor.show()
    }

    private func showTrace(_ id: String) {
        guard let service = hub.snapshot().services.first(where: { $0.id == id }) else { return }
        EventLogWindow.show(hub:hub,service:service)
    }

    private func confirmDelete(_ id: String) {
        guard let service = hub.snapshot().services.first(where: { $0.id == id }) else { return }
        let alert = ConnectionDeletion.alert(for: service)
        guard alert.runModal() == .alertSecondButtonReturn else { return }
        do {
            try hub.remove(id: id)
            refresh()
        } catch {
            showError("Could not delete connection", error.localizedDescription)
        }
    }

    private func showError(_ title: String, _ body: String) {
        let alert = Brand.alert()
        alert.messageText = title
        alert.informativeText = body
        alert.runModal()
    }
}

@MainActor
private final class ConnectionsRootView: NSView {
    private let title: NSTextField
    private let subtitle: NSTextField
    private let icon = NSImageView()
    private let agents: NSButton
    private let more: NSPopUpButton
    private let addButton: NSButton
    private let scrollView: NSScrollView

    init(title: NSTextField, subtitle: NSTextField, addButton: NSButton, agents:NSButton, more:NSPopUpButton, scrollView: NSScrollView) {
        self.title = title
        self.subtitle = subtitle
        self.addButton = addButton
        self.agents = agents; self.more = more
        self.scrollView = scrollView
        super.init(frame: NSRect(x: 0, y: 0, width: 820, height: 610))
        autoresizingMask = [.width, .height]
        icon.image = Brand.logo; icon.imageScaling = .scaleProportionallyUpOrDown; addSubview(icon)
        addSubview(agents); addSubview(more)
        addSubview(title)
        addSubview(subtitle)
        addSubview(addButton)
        addSubview(scrollView)
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    func setCounts(_ total: Int, listening: Int) {
        subtitle.stringValue = total == 0 ? "Connect a service or an agent to this Mac." : "\(total) paired · \(listening) listening now"
        needsLayout = true
    }

    override func layout() {
        super.layout()
        let width = bounds.width
        icon.frame = NSRect(x:20,y:13,width:54,height:54)
        title.frame = NSRect(x:84,y:15,width:width - 110,height:30)
        subtitle.frame = NSRect(x:85,y:47,width:width - 110,height:18)
        let actionsY: CGFloat = 77
        let size = addButton.fittingSize
        addButton.frame = NSRect(x:20,y:actionsY,width:size.width,height:30)
        let agentWidth = agents.fittingSize.width
        agents.frame = NSRect(x:addButton.frame.maxX+6,y:actionsY,width:agentWidth,height:30)
        let toolWidth = more.fittingSize.width
        more.frame = NSRect(x:width-toolWidth-20,y:actionsY,width:toolWidth,height:30)
        let listTop: CGFloat = 121
        let listHeight = max(0, bounds.height - listTop - 20)
        scrollView.frame = NSRect(x: 20, y: listTop, width: max(0, width - 40), height: listHeight)
        (scrollView.documentView as? ConnectionDocumentView)?.resizeTo(width: scrollView.contentView.bounds.width, viewportHeight: scrollView.contentView.bounds.height)
    }
}

enum ConnectionSection: String, CaseIterable {
    case dot = "Dot"
    case workflows = "Workflow services"
    case agents = "Agent sessions"

    init(_ role: ConnectionRole) {
        switch role {
        case .codexSession, .claudeSession: self = .agents
        case .dotSession: self = .dot
        case .workflow: self = .workflows
        }
    }
}

@MainActor
enum ConnectionDeletion {
    static func alert(for service: ServiceView) -> NSAlert {
        let alert = Brand.alert()
        alert.alertStyle = .warning
        alert.messageText = "Delete " + service.name + "?"
        let consequence: String
        let action: String
        switch ConnectionSection(service.role) {
        case .agents:
            action = "Delete Registration"
            consequence = "This chat will stop receiving D-Bond messages. To reconnect, register it with a fresh alias, or register a new chat."
        case .dot:
            action = "Delete Dot Pairing"
            consequence = "Workflows that address this Dot pairing will lose their route. To restore it, pair Dot again and update each workflow with its new address."
        case .workflows:
            action = "Delete Workflow Connection"
            consequence = "Tools that use this connection will stop exchanging messages. To reconnect, create a new pairing, re-enter its rules and update the tools with its new address or credential."
        }
        var details = "D-Bond revokes this credential and deletes this pairing’s rules, messages and receipts."
        if service.queued > 0 { details += " Queued events to remove: " + String(service.queued) + "." }
        if service.inFlight > 0 { details += " A delivery already in progress may finish." }
        if !service.paused { details += " Use Pause to keep the pairing for later." }
        alert.informativeText = consequence + "\n\n" + details
        alert.addButton(withTitle: "Cancel")
        let delete = alert.addButton(withTitle: action)
        delete.hasDestructiveAction = true
        delete.keyEquivalent = ""
        return alert
    }
}

@MainActor
private final class ConnectionDocumentView: NSView {
    private var sections: [ConnectionSection: [ConnectionRowView]] = [:]
    private var headings: [ConnectionSection: NSTextField] = [:]
    private var emptyLabels: [ConnectionSection: NSTextField] = [:]
    private let connector = NSButton(title: "Dot settings…", target: nil, action: nil)
    private let connectorLabel = NSTextField(labelWithString: "Shared connector for all Dot traffic")
    private let onManageDot: () -> Void

    init(onManageDot: @escaping () -> Void) {
        self.onManageDot = onManageDot
        super.init(frame: .zero)
        for section in ConnectionSection.allCases {
            let heading = NSTextField(labelWithString: section.rawValue)
            heading.font = .systemFont(ofSize: 16, weight: .semibold)
            heading.setAccessibilityRole(.staticText)
            headings[section] = heading
            addSubview(heading)
            if section != .dot {
                let text = section == .agents ? "No Claude or Codex sessions registered." : "No workflow services paired."
                let label = NSTextField(labelWithString: text)
                label.textColor = .secondaryLabelColor
                emptyLabels[section] = label
                addSubview(label)
            }
        }
        connector.target = self
        connector.action = #selector(manageDot)
        connector.bezelStyle = .rounded
        connector.setAccessibilityLabel("Manage shared Dot connector")
        connectorLabel.textColor = .secondaryLabelColor
        connectorLabel.lineBreakMode = .byTruncatingTail
        addSubview(connectorLabel)
        addSubview(connector)
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    @objc private func manageDot() { onManageDot() }

    func setDotStatus(_ status: String) {
        connectorLabel.stringValue = status
        connectorLabel.toolTip = status
    }

    func setSections(_ sections: [ConnectionSection: [ConnectionRowView]]) {
        self.sections = sections
        for rows in sections.values {
            for row in rows where row.superview !== self { addSubview(row) }
        }
        if let scroll = enclosingScrollView {
            resizeTo(width: scroll.contentView.bounds.width, viewportHeight: scroll.contentView.bounds.height)
        }
    }

    func resizeTo(width: CGFloat, viewportHeight: CGFloat) {
        let width = max(0, width)
        var top: CGFloat = 0
        for section in ConnectionSection.allCases {
            headings[section]?.frame = NSRect(x: 0, y: top, width: width, height: 22)
            top += 28
            if section == .dot {
                let buttonWidth = connector.fittingSize.width
                connector.frame = NSRect(x: max(0, width - buttonWidth), y: top - 2, width: buttonWidth, height: 26)
                connectorLabel.frame = NSRect(x: 1, y: top + 3, width: max(0, width - buttonWidth - 12), height: 18)
                top += 30
            }
            let rows = sections[section, default: []]
            if let label = emptyLabels[section] {
                label.isHidden = !rows.isEmpty
                if rows.isEmpty {
                    label.frame = NSRect(x: 1, y: top, width: width, height: 20)
                    top += 20
                }
            }
            for row in rows {
                row.frame = NSRect(x: 0, y: top, width: width, height: 84)
                row.layoutContent()
                top += 90
            }
            top += 14
        }
        frame = NSRect(x: 0, y: 0, width: width, height: max(viewportHeight, top))
    }
}

@MainActor
private final class ConnectionRowView: NSView {
    private let title = HoverMarqueeLabel()
    private let statusDot = StatusDotView()
    private let status = NSTextField(labelWithString: "")
    private let detail = NSTextField(labelWithString: "")
    private let pauseButton = NSButton(title: "Pause", target: nil, action: nil)
    private let deleteButton = NSButton(title: "Delete", target: nil, action: nil)
    private let logo = NSImageView()
    private let setupButton = NSButton(title: "Setup", target: nil, action: nil)
    private let sopButton = NSButton(title: "Rules", target: nil, action: nil)
    private let traceButton = NSButton(title: "Events", target: nil, action: nil)
    private let onPause: (String) -> Void
    private let onDelete: (String) -> Void
    private let onSetup: (String) -> Void
    private let onSOP: (String) -> Void
    private let onTrace: (String) -> Void
    private var serviceID = ""
    private let statusWidth: CGFloat = 110
    private let pauseWidth: CGFloat = 62
    private let deleteWidth: CGFloat = 62

    init(service: ServiceView, onPause: @escaping (String) -> Void, onDelete: @escaping (String) -> Void, onSetup: @escaping (String) -> Void, onSOP: @escaping (String) -> Void, onTrace: @escaping (String) -> Void) {
        self.onPause = onPause
        self.onDelete = onDelete
        self.onSetup = onSetup
        self.onSOP = onSOP
        self.onTrace = onTrace
        super.init(frame: NSRect(x: 0, y: 0, width: 680, height: 112))
        wantsLayer = true
        layer?.cornerRadius = 11
        layer?.borderWidth = 1
        layer?.borderColor = NSColor.separatorColor.withAlphaComponent(0.45).cgColor
        layer?.backgroundColor = NSColor.controlBackgroundColor.withAlphaComponent(0.58).cgColor
        title.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        status.font = .systemFont(ofSize: 11, weight: .medium)
        statusDot.color = .systemGreen
        logo.imageScaling = .scaleProportionallyUpOrDown; addSubview(logo)
        addSubview(title)
        addSubview(statusDot)
        addSubview(status)
        addSubview(detail)
        addSubview(pauseButton)
        addSubview(deleteButton)
        for button in [setupButton, sopButton, traceButton] {
            button.bezelStyle = .rounded
            button.controlSize = .small
            button.target = self
            addSubview(button)
        }
        setupButton.action = #selector(showSetup)
        sopButton.action = #selector(editRules)
        traceButton.action = #selector(showEvents)
        pauseButton.bezelStyle = .rounded
        pauseButton.controlSize = .small
        pauseButton.target = self
        pauseButton.action = #selector(togglePause)
        deleteButton.bezelStyle = .rounded
        deleteButton.controlSize = .small
        deleteButton.target = self
        deleteButton.action = #selector(deleteConnection)
        deleteButton.contentTintColor = .systemRed
        detail.font = .systemFont(ofSize: 11)
        detail.textColor = .secondaryLabelColor
        detail.lineBreakMode = .byTruncatingTail
        update(service)
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    func layoutContent() {
        let left: CGFloat = 65
        let right: CGFloat = 14
        let top: CGFloat = 8
        logo.frame = NSRect(x:13,y:13,width:39,height:39)
        let controlsWidth = statusWidth + pauseWidth + deleteWidth + 22
        let titleWidth = max(100, bounds.width - left - right - controlsWidth - 16)
        title.frame = NSRect(x: left, y: top + 1, width: titleWidth, height: 23)
        let controlsX = bounds.width - right - controlsWidth
        statusDot.frame = NSRect(x: controlsX, y: top + 8, width: 8, height: 8)
        status.frame = NSRect(x: controlsX + 14, y: top + 2, width: statusWidth - 14, height: 20)
        pauseButton.frame = NSRect(x: controlsX + statusWidth, y: top - 1, width: pauseWidth, height: 26)
        deleteButton.frame = NSRect(x: controlsX + statusWidth + pauseWidth + 6, y: top - 1, width: deleteWidth, height: 26)
        detail.frame = NSRect(x: left, y: top + 26, width: max(80, bounds.width - left - right), height: 18)
        let setupWidth: CGFloat = setupButton.title == "Setup" ? 64 : 126
        setupButton.frame = NSRect(x: left, y: top + 49, width: setupWidth, height: 25)
        sopButton.frame = NSRect(x: left + setupWidth + 8, y: top + 49, width: 64, height: 25)
        traceButton.frame = NSRect(x: left + setupWidth + 80, y: top + 49, width: 64, height: 25)
    }

    func update(_ service: ServiceView) {
        serviceID = service.id
        logo.image = Brand.service(service)
        if title.text != service.name { title.text = service.name }
        let stateText: String
        let color: NSColor
        let agent = AgentSessionKind(service: service) != nil
        if service.paused {
            stateText = "Paused"
            color = .tertiaryLabelColor
        } else if agent {
            stateText = SessionWakeStatus.label(service)
            color = stateText == "Wake verified" ? .systemGreen : .systemOrange
        } else if service.subscribers + service.directSubscribers > 0 {
            stateText = "Listening"
            color = .systemGreen
        } else {
            stateText = "Paired"
            color = .systemOrange
        }
        status.stringValue = stateText
        status.textColor = color
        statusDot.color = color
        pauseButton.title = service.paused ? "Resume" : "Pause"
        pauseButton.toolTip = service.paused ? "Resume this connection" : "Pause this connection"
        let destination = ["dot": "Dot", "peer": "Direct sessions", "herald": "Herald", "demo": "Local echo"][service.destination] ?? service.destination
        var streams: [String] = []
        if service.subscribers > 0 { streams.append("Dot messages") }
        if service.directSubscribers > 0 { streams.append("Direct messages") }
        let activity: String
        if agent {
            let direct = !service.paused && service.directSubscribers > 0 ? "online" : "offline"
            let dot = service.destination == "peer" ? "not used" : (!service.paused && service.subscribers > 0 ? "online" : "offline")
            activity = "Direct listener \(direct) · Dot stream \(dot) · \(SessionWakeStatus.detail(service))"
        } else {
            activity = streams.isEmpty ? "No active listener" : "Listening for " + streams.joined(separator:" and ").lowercased()
        }
        let pending = service.queued + service.inFlight
        let queue = pending == 0 ? "" : " · \(pending) queued or dispatching"
        let seen = service.lastSeen.map { " · last event \(displayTraceTime($0))" } ?? ""
        detail.stringValue = "\(destination) · \(activity)\(queue)\(seen)"
        detail.toolTip = detail.stringValue
        setupButton.title = agent ? "Connect session…" : "Setup"
        setupButton.toolTip = agent ? "Connect this exact paired agent session" : "View this service's setup"
        sopButton.title = "Rules \(service.sop?.rules.count ?? 0)"
        setAccessibilityElement(true)
        setAccessibilityLabel("\(service.name), \(stateText), destination \(destination), \(activity)")
        layoutContent()
    }

    @objc private func togglePause() { onPause(serviceID) }
    @objc private func deleteConnection() { onDelete(serviceID) }
    @objc private func showSetup() { onSetup(serviceID) }
    @objc private func editRules() { onSOP(serviceID) }
    @objc private func showEvents() { onTrace(serviceID) }
}

@MainActor
private final class HoverMarqueeLabel: NSView {
    var text: String = "" {
        didSet {
            offset = 0
            hoverTimer?.invalidate()
            setAccessibilityElement(true)
            setAccessibilityRole(.staticText)
            setAccessibilityLabel(text)
            setAccessibilityHelp("Hover to scroll and reveal the full connection name.")
            toolTip = text
            needsDisplay = true
        }
    }
    private var offset: CGFloat = 0
    private var direction: CGFloat = 1
    private var hoverTimer: Timer?
    private var dwell = 0
    private var tracking: NSTrackingArea?
    private let font = NSFont.systemFont(ofSize: 14, weight: .semibold)
    override var isFlipped: Bool { true }
    override var acceptsFirstResponder: Bool { false }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let tracking { removeTrackingArea(tracking) }
        let area = NSTrackingArea(rect: bounds, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect], owner: self, userInfo: nil)
        addTrackingArea(area)
        tracking = area
    }

    override func mouseEntered(with event: NSEvent) {
        guard measuredWidth > bounds.width else { return }
        offset = 0
        direction = 1
        dwell = 0
        hoverTimer?.invalidate()
        hoverTimer = Timer.scheduledTimer(withTimeInterval: 0.025, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.advanceMarquee() }
        }
    }

    override func mouseExited(with event: NSEvent) {
        hoverTimer?.invalidate()
        hoverTimer = nil
        offset = 0
        needsDisplay = true
    }

    private var measuredWidth: CGFloat { (text as NSString).size(withAttributes: [.font: font]).width }

    private func advanceMarquee() {
        let maxOffset = max(0, measuredWidth - bounds.width + 8)
        if dwell > 20 {
            offset += direction * 0.7
            if offset >= maxOffset { offset = maxOffset; direction = -1; dwell = 0 }
            else if offset <= 0 { offset = 0; direction = 1; dwell = 0 }
        } else { dwell += 1 }
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        guard !text.isEmpty else { return }
        NSGraphicsContext.saveGraphicsState()
        NSBezierPath(rect: bounds).addClip()
        let y = (bounds.height - (font.ascender - font.descender)) / 2 - font.descender
        (text as NSString).draw(at: NSPoint(x: bounds.minX - offset, y: y), withAttributes: [
            .font: font,
            .foregroundColor: NSColor.labelColor,
        ])
        NSGraphicsContext.restoreGraphicsState()
    }
}

@MainActor
private final class StatusDotView: NSView {
    var color: NSColor = .systemGreen { didSet { needsDisplay = true } }
    override var isFlipped: Bool { true }
    override func draw(_ dirtyRect: NSRect) {
        color.setFill()
        NSBezierPath(ovalIn: bounds.insetBy(dx: 1, dy: 1)).fill()
    }
}
