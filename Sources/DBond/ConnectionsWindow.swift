import AppKit
import DBondCore

/// A reusable connections window. Its one-second refresh reads only Hub's local snapshot.
@MainActor
final class ConnectionsWindowController: NSObject, NSWindowDelegate {
    private let hub: Hub
    private let onAdd: () -> Void
    private let onAgents, onLog, onBridge, onDotSetup: () -> Void
    private let onSetup: (ServiceView) -> Void
    private var window: NSWindow?
    private var rootView: ConnectionsRootView?
    private var documentView: ConnectionDocumentView?
    private var rows: [String: ConnectionRowView] = [:]
    private var refreshTimer: Timer?
    private var ruleEditors: [String: RuleEditor] = [:]

    init(hub: Hub, onAdd: @escaping () -> Void, onSetup: @escaping (ServiceView) -> Void, onAgents: @escaping () -> Void, onLog: @escaping () -> Void, onBridge: @escaping () -> Void, onDotSetup: @escaping () -> Void) {
        self.hub = hub
        self.onAdd = onAdd
        self.onAgents = onAgents; self.onLog = onLog; self.onBridge = onBridge; self.onDotSetup = onDotSetup
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
        let document = ConnectionDocumentView()
        scroll.documentView = document
        self.documentView = document

        let empty = NSTextField(wrappingLabelWithString: "No connections yet. Add a connection to create a local endpoint.")
        empty.font = .systemFont(ofSize: 13)
        empty.textColor = .secondaryLabelColor
        empty.alignment = .center

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
        let root = ConnectionsRootView(title: title, subtitle: subtitle, addButton: add, agents:agents, more:more, scrollView: scroll, emptyLabel: empty)
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
        let orderedRows = services.map { service -> ConnectionRowView in
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
            return row
        }
        documentView.setRows(orderedRows)
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
        let alert = Brand.alert()
        alert.alertStyle = .warning
        alert.messageText = "Delete \(service.name)?"
        alert.informativeText = "This revokes its local credential and deletes its queued events and receipts. A delivery already dispatching may still finish."
        alert.addButton(withTitle: "Delete Connection")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
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
    private let emptyLabel: NSTextField

    init(title: NSTextField, subtitle: NSTextField, addButton: NSButton, agents:NSButton, more:NSPopUpButton, scrollView: NSScrollView, emptyLabel: NSTextField) {
        self.title = title
        self.subtitle = subtitle
        self.addButton = addButton
        self.agents = agents; self.more = more
        self.scrollView = scrollView
        self.emptyLabel = emptyLabel
        super.init(frame: NSRect(x: 0, y: 0, width: 820, height: 610))
        autoresizingMask = [.width, .height]
        icon.image = Brand.app; icon.imageScaling = .scaleProportionallyUpOrDown; addSubview(icon)
        addSubview(agents); addSubview(more)
        addSubview(title)
        addSubview(subtitle)
        addSubview(addButton)
        addSubview(scrollView)
        addSubview(emptyLabel)
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    func setCounts(_ total: Int, listening: Int) {
        subtitle.stringValue = total == 0 ? "Connect a service or an agent to this Mac." : "\(total) paired · \(listening) listening now"
        emptyLabel.isHidden = total != 0
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
        emptyLabel.frame = NSRect(x: 40, y: listTop + max(0, (listHeight - 42) / 2), width: max(0, width - 80), height: 42)
        (scrollView.documentView as? ConnectionDocumentView)?.resizeTo(width: scrollView.contentView.bounds.width, viewportHeight: scrollView.contentView.bounds.height)
    }
}

@MainActor
private final class ConnectionDocumentView: NSView {
    private var rows: [ConnectionRowView] = []
    private let rowHeight: CGFloat = 92
    private let rowGap: CGFloat = 6
    override var isFlipped: Bool { true }

    func setRows(_ rows: [ConnectionRowView]) {
        for old in self.rows where !rows.contains(where: { $0 === old }) { old.removeFromSuperview() }
        self.rows = rows
        for row in rows where row.superview !== self { addSubview(row) }
        if let scroll = enclosingScrollView {
            resizeTo(width: scroll.contentView.bounds.width, viewportHeight: scroll.contentView.bounds.height)
        }
    }

    func resizeTo(width: CGFloat, viewportHeight: CGFloat) {
        let rowsHeight = rows.isEmpty ? 0 : CGFloat(rows.count) * rowHeight + CGFloat(rows.count - 1) * rowGap
        let height = max(viewportHeight, rowsHeight)
        frame = NSRect(x: 0, y: 0, width: max(0, width), height: height)
        for (index, row) in rows.enumerated() {
            row.frame = NSRect(x: 0, y: CGFloat(index) * (rowHeight + rowGap), width: max(0, width), height: rowHeight)
            row.layoutContent()
        }
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
