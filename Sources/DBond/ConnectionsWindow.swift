import AppKit
import DBondCore

/// A reusable connections window. Its one-second refresh reads only Hub's local snapshot.
@MainActor
final class ConnectionsWindowController: NSObject, NSWindowDelegate {
    private let hub: Hub
    private let onAdd: () -> Void
    private let onSetup: (ServiceView) -> Void
    private var window: NSWindow?
    private var rootView: ConnectionsRootView?
    private var documentView: ConnectionDocumentView?
    private var rows: [String: ConnectionRowView] = [:]
    private var refreshTimer: Timer?

    init(hub: Hub, onAdd: @escaping () -> Void, onSetup: @escaping (ServiceView) -> Void) {
        self.hub = hub
        self.onAdd = onAdd
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
            contentRect: NSRect(x: 0, y: 0, width: 760, height: 540),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Connections"
        window.minSize = NSSize(width: 620, height: 380)
        window.isReleasedWhenClosed = false
        window.appearance = NSAppearance(named: .darkAqua)
        window.delegate = self

        let title = NSTextField(labelWithString: "Connections")
        title.font = .systemFont(ofSize: 23, weight: .semibold)
        let subtitle = NSTextField(labelWithString: "Pairing creates a local endpoint. A producer must be configured separately before events arrive.")
        subtitle.font = .systemFont(ofSize: 12)
        subtitle.textColor = .secondaryLabelColor
        let add = NSButton(title: "Add Connection…", target: self, action: #selector(addConnection))
        add.bezelStyle = .rounded
        add.controlSize = .regular

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

        let root = ConnectionsRootView(title: title, subtitle: subtitle, addButton: add, scrollView: scroll, emptyLabel: empty)
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
        rootView.setEmpty(services.isEmpty)
    }

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
        onSetup(service)
    }

    private func editSOP(_ id: String) {
        guard let service = hub.snapshot().services.first(where: { $0.id == id }) else { return }
        let oldRules = service.sop?.rules ?? []
        var rules = oldRules
        while true {
            let alert = NSAlert()
            alert.messageText = "Instructions for \(service.name)"
            alert.informativeText = "Choose one rule to edit or delete. Rule text may contain multiple paragraphs. Changes are saved together as a new version for future events."
            alert.addButton(withTitle: "Edit Selected")
            alert.addButton(withTitle: "Add Rule")
            alert.addButton(withTitle: "Delete Selected")
            alert.addButton(withTitle: "Save and Close")
            alert.addButton(withTitle: "Cancel")
            let picker = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 480, height: 28), pullsDown: false)
            if rules.isEmpty { picker.addItem(withTitle: "No rules yet") }
            for (index, rule) in rules.enumerated() {
                let preview = rule.text.replacingOccurrences(of: "\n", with: " ")
                picker.addItem(withTitle: "\(index + 1). \(String(preview.prefix(65)))")
            }
            alert.buttons[0].isEnabled = !rules.isEmpty
            alert.buttons[2].isEnabled = !rules.isEmpty
            alert.accessoryView = picker
            let choice = alert.runModal().rawValue - NSApplication.ModalResponse.alertFirstButtonReturn.rawValue
            switch choice {
            case 0:
                let index = picker.indexOfSelectedItem
                guard rules.indices.contains(index) else { continue }
                if let text = editRuleText(rules[index].text, title: "Edit rule \(index + 1)") { rules[index].text = text }
            case 1:
                if let text = editRuleText("", title: "Add rule") { rules.append(OwnerRule(id: UUID().uuidString, text: text)) }
            case 2:
                let index = picker.indexOfSelectedItem
                guard rules.indices.contains(index) else { continue }
                rules.remove(at: index)
            case 3:
                guard rules != oldRules else { return }
                let next = OwnerSOP(version: (service.sop?.version ?? 0) + 1, rules: rules)
                do { try hub.setSOP(service: id, sop: next, expectedVersion: service.sop?.version ?? 0); refresh() }
                catch { showError("Could not save rules", error.localizedDescription) }
                return
            default:
                return
            }
        }
    }

    private func editRuleText(_ original: String, title: String) -> String? {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = "Paste or edit the complete text. Paragraphs and line breaks belong to this one rule."
        alert.addButton(withTitle: "Keep Rule")
        alert.addButton(withTitle: "Cancel")
        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 580, height: 350))
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        let editor = NSTextView(frame: scroll.bounds)
        editor.isRichText = false
        editor.isAutomaticQuoteSubstitutionEnabled = false
        editor.isAutomaticDashSubstitutionEnabled = false
        editor.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        editor.string = original
        scroll.documentView = editor
        alert.accessoryView = scroll
        guard alert.runModal() == .alertFirstButtonReturn else { return nil }
        guard !editor.string.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            showError("Rule is empty", "Use Delete Selected to remove a rule.")
            return nil
        }
        return editor.string
    }

    private func showTrace(_ id: String) {
        guard let service = hub.snapshot().services.first(where: { $0.id == id }) else { return }
        do {
            let traces = try hub.recentEventTraces(service: id, limit: 10)
            let body = traces.isEmpty ? "No events recorded for this connection yet." : traces.map { trace in
                let stages = trace.stages.map { "  \(displayTraceTime($0.at))  \($0.state)  [\($0.basis)]" }.joined(separator: "\n")
                let version = trace.sopVersion.map { " · rules v\($0)" } ?? ""
                return "\(trace.eventID)\(version)\n\(stages)"
            }.joined(separator: "\n\n")
            let alert = NSAlert()
            alert.messageText = "Recent events · \(service.name)"
            alert.informativeText = "Receipt times use this Mac’s timezone. broker_recorded is local state; native_observed is an observed native response. Neither alone proves a final reply."
            alert.addButton(withTitle: "Done")
            alert.addButton(withTitle: "Copy trace")
            let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 560, height: 300))
            scroll.hasVerticalScroller = true
            scroll.borderType = .bezelBorder
            let text = NSTextView(frame: scroll.bounds)
            text.isEditable = false
            text.isRichText = false
            text.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
            text.string = body
            scroll.documentView = text
            alert.accessoryView = scroll
            if alert.runModal() == .alertSecondButtonReturn {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(body, forType: .string)
            }
        } catch { showError("Could not read event trace", error.localizedDescription) }
    }

    private func confirmDelete(_ id: String) {
        guard let service = hub.snapshot().services.first(where: { $0.id == id }) else { return }
        let alert = NSAlert()
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
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = body
        alert.runModal()
    }
}

@MainActor
private final class ConnectionsRootView: NSView {
    private let title: NSTextField
    private let subtitle: NSTextField
    private let addButton: NSButton
    private let scrollView: NSScrollView
    private let emptyLabel: NSTextField

    init(title: NSTextField, subtitle: NSTextField, addButton: NSButton, scrollView: NSScrollView, emptyLabel: NSTextField) {
        self.title = title
        self.subtitle = subtitle
        self.addButton = addButton
        self.scrollView = scrollView
        self.emptyLabel = emptyLabel
        super.init(frame: NSRect(x: 0, y: 0, width: 760, height: 540))
        autoresizingMask = [.width, .height]
        addSubview(title)
        addSubview(subtitle)
        addSubview(addButton)
        addSubview(scrollView)
        addSubview(emptyLabel)
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    func setEmpty(_ empty: Bool) {
        emptyLabel.isHidden = !empty
        needsLayout = true
    }

    override func layout() {
        super.layout()
        let width = bounds.width
        title.frame = NSRect(x: 24, y: 20, width: max(220, width - 240), height: 29)
        subtitle.frame = NSRect(x: 25, y: 53, width: max(0, width - 50), height: 18)
        let buttonSize = addButton.fittingSize
        addButton.frame = NSRect(x: width - 24 - buttonSize.width, y: 19, width: buttonSize.width, height: max(30, buttonSize.height))
        let listTop: CGFloat = 88
        let listHeight = max(0, bounds.height - listTop - 20)
        scrollView.frame = NSRect(x: 20, y: listTop, width: max(0, width - 40), height: listHeight)
        emptyLabel.frame = NSRect(x: 40, y: listTop + max(0, (listHeight - 42) / 2), width: max(0, width - 80), height: 42)
        (scrollView.documentView as? ConnectionDocumentView)?.resizeTo(width: scrollView.contentView.bounds.width, viewportHeight: scrollView.contentView.bounds.height)
    }
}

@MainActor
private final class ConnectionDocumentView: NSView {
    private var rows: [ConnectionRowView] = []
    private let rowHeight: CGFloat = 112
    private let rowGap: CGFloat = 10
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
    private let setupButton = NSButton(title: "Setup", target: nil, action: nil)
    private let sopButton = NSButton(title: "Rules", target: nil, action: nil)
    private let traceButton = NSButton(title: "Events", target: nil, action: nil)
    private let onPause: (String) -> Void
    private let onDelete: (String) -> Void
    private let onSetup: (String) -> Void
    private let onSOP: (String) -> Void
    private let onTrace: (String) -> Void
    private var serviceID = ""
    private let statusWidth: CGFloat = 92
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
        let left: CGFloat = 14
        let right: CGFloat = 14
        let top: CGFloat = 12
        let controlsWidth = statusWidth + pauseWidth + deleteWidth + 22
        let titleWidth = max(100, bounds.width - left - right - controlsWidth - 16)
        title.frame = NSRect(x: left, y: top + 1, width: titleWidth, height: 23)
        let controlsX = bounds.width - right - controlsWidth
        statusDot.frame = NSRect(x: controlsX, y: top + 8, width: 8, height: 8)
        status.frame = NSRect(x: controlsX + 14, y: top + 2, width: statusWidth - 14, height: 20)
        pauseButton.frame = NSRect(x: controlsX + statusWidth, y: top - 1, width: pauseWidth, height: 26)
        deleteButton.frame = NSRect(x: controlsX + statusWidth + pauseWidth + 6, y: top - 1, width: deleteWidth, height: 26)
        detail.frame = NSRect(x: left, y: top + 35, width: max(80, bounds.width - left - right), height: 18)
        setupButton.frame = NSRect(x: left, y: top + 64, width: 64, height: 25)
        sopButton.frame = NSRect(x: left + 72, y: top + 64, width: 64, height: 25)
        traceButton.frame = NSRect(x: left + 144, y: top + 64, width: 64, height: 25)
    }

    func update(_ service: ServiceView) {
        serviceID = service.id
        if title.text != service.name { title.text = service.name }
        let stateText: String
        let color: NSColor
        if service.paused {
            stateText = "Paused"
            color = .tertiaryLabelColor
        } else if service.subscribers > 0 {
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
        let destination = ["dot": "Dot", "herald": "Herald", "demo": "Local echo"][service.destination] ?? service.destination
        let activity = service.subscribers == 0 ? "No open return stream" : "\(service.subscribers) return stream\(service.subscribers == 1 ? "" : "s")"
        let pending = service.queued + service.inFlight
        let queue = pending == 0 ? "" : " · \(pending) queued or dispatching"
        let seen = service.lastSeen.map { " · last event \(displayTraceTime($0))" } ?? ""
        detail.stringValue = "\(destination) · \(activity)\(queue)\(seen)"
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
