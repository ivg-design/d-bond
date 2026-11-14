import AppKit
import DBondCore

/// One window per connection. Every page is copied from the ledger off the UI thread.
@MainActor
final class EventLogWindow: NSObject, NSWindowDelegate {
    private static var openWindows: [String: EventLogWindow] = [:]
    static func show(hub: Hub, service: ServiceView) {
        if let existing = openWindows[service.id] {
            existing.window.makeKeyAndOrderFront(nil)
            return
        }
        let controller = EventLogWindow(hub: hub, service: service)
        openWindows[service.id] = controller
        controller.window.center()
        controller.window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        controller.loadPage()
    }

    private let hub: Hub
    private let service: ServiceView
    private let window: NSWindow
    private let textView = NSTextView()
    private let scope = NSTextField(labelWithString: "Recent events")
    private let count = NSTextField(labelWithString: "Loading…")
    private let fullButton = NSButton(title: "View complete log", target: nil, action: nil)
    private let olderButton = NSButton(title: "Older events", target: nil, action: nil)
    private let newerButton = NSButton(title: "Newer events", target: nil, action: nil)
    private let copyButton = NSButton(title: "Copy shown page", target: nil, action: nil)
    private var refreshScheduled = false
    private var full = false
    private var snapshotLastEventID: String?
    private var beforeCursors: [String?] = [nil]
    private var nextBeforeEventID: String?
    private var generation = 0
    private var shownText = ""

    private init(hub: Hub, service: ServiceView) {
        self.hub = hub; self.service = service
        window = NSWindow(contentRect: NSRect(x:0,y:0,width:780,height:600),
                          styleMask:[.titled,.closable,.miniaturizable,.resizable],backing:.buffered,defer:false)
        super.init()
        window.title = "D-Bond · Events · \(service.name)"
        window.minSize = NSSize(width:590,height:380)
        window.isReleasedWhenClosed = false
        window.appearance = NSAppearance(named:.darkAqua)
        window.delegate = self
        build()
        NotificationCenter.default.addObserver(self, selector: #selector(ledgerUpdated(_:)),
                                               name: .dbondLedgerUpdated, object: nil)
    }

    @objc private func ledgerUpdated(_ note: Notification) {
        guard let changed = note.object as? Hub, changed === hub, !full, !refreshScheduled else { return }
        refreshScheduled = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { [weak self] in
            guard let self else { return }
            self.refreshScheduled = false
            if !self.full && self.window.isVisible { self.loadPage(live: true) }
        }
    }

    private func build() {
        let root = NSView()
        root.wantsLayer = true
        root.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        window.contentView = root
        let icon = NSImageView()
        icon.image = Brand.logo
        icon.imageScaling = .scaleProportionallyUpOrDown
        let title = NSTextField(labelWithString:"Events · \(service.name)")
        title.font = .systemFont(ofSize:21,weight:.semibold)
        title.lineBreakMode = .byTruncatingTail
        let brand = NSTextField(labelWithString:"D-BOND")
        brand.font = .systemFont(ofSize:10,weight:.bold)
        brand.textColor = .secondaryLabelColor
        let heading = NSStackView(views:[brand,title])
        heading.orientation = .vertical; heading.alignment = .leading; heading.spacing = 2
        let header = NSStackView(views:[icon,heading])
        header.orientation = .horizontal; header.alignment = .centerY; header.spacing = 12
        let explanation = NSTextField(wrappingLabelWithString:
            "Times are local to this Mac.")
        explanation.font = .systemFont(ofSize:12)
        explanation.textColor = .secondaryLabelColor
        scope.font = .systemFont(ofSize:13,weight:.semibold)
        count.font = .systemFont(ofSize:12)
        count.textColor = .secondaryLabelColor
        fullButton.target = self; fullButton.action = #selector(openFull)
        fullButton.bezelStyle = .rounded
        fullButton.image = NSImage(systemSymbolName:"clock.arrow.circlepath",accessibilityDescription:nil)
        fullButton.imagePosition = .imageLeading
        let scopeRow = NSStackView(views:[scope,count,NSView(),fullButton])
        scopeRow.orientation = .horizontal; scopeRow.alignment = .centerY; scopeRow.spacing = 10
        textView.isEditable = false
        textView.isSelectable = true
        textView.isRichText = false
        textView.font = .monospacedSystemFont(ofSize:12,weight:.regular)
        textView.textColor = .labelColor
        textView.backgroundColor = .controlBackgroundColor
        textView.textContainerInset = NSSize(width:14,height:12)
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]
        textView.textContainer?.widthTracksTextView = true
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        scroll.documentView = textView
        olderButton.target = self; olderButton.action = #selector(older)
        newerButton.target = self; newerButton.action = #selector(newer)
        copyButton.target = self; copyButton.action = #selector(copyPage)
        let controls = NSStackView(views:[newerButton,olderButton,NSView(),copyButton])
        controls.orientation = .horizontal; controls.alignment = .centerY; controls.spacing = 9
        for view in [header,explanation,scopeRow,scroll,controls] { view.translatesAutoresizingMaskIntoConstraints = false; root.addSubview(view) }
        icon.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            icon.widthAnchor.constraint(equalToConstant:38),icon.heightAnchor.constraint(equalToConstant:38),
            header.topAnchor.constraint(equalTo:root.topAnchor,constant:20),
            header.leadingAnchor.constraint(equalTo:root.leadingAnchor,constant:22),
            header.trailingAnchor.constraint(lessThanOrEqualTo:root.trailingAnchor,constant:-22),
            explanation.topAnchor.constraint(equalTo:header.bottomAnchor,constant:12),
            explanation.leadingAnchor.constraint(equalTo:root.leadingAnchor,constant:22),
            explanation.trailingAnchor.constraint(equalTo:root.trailingAnchor,constant:-22),
            scopeRow.topAnchor.constraint(equalTo:explanation.bottomAnchor,constant:16),
            scopeRow.leadingAnchor.constraint(equalTo:root.leadingAnchor,constant:22),
            scopeRow.trailingAnchor.constraint(equalTo:root.trailingAnchor,constant:-22),
            scroll.topAnchor.constraint(equalTo:scopeRow.bottomAnchor,constant:10),
            scroll.leadingAnchor.constraint(equalTo:root.leadingAnchor,constant:22),
            scroll.trailingAnchor.constraint(equalTo:root.trailingAnchor,constant:-22),
            controls.topAnchor.constraint(equalTo:scroll.bottomAnchor,constant:12),
            controls.leadingAnchor.constraint(equalTo:root.leadingAnchor,constant:22),
            controls.trailingAnchor.constraint(equalTo:root.trailingAnchor,constant:-22),
            controls.bottomAnchor.constraint(equalTo:root.bottomAnchor,constant:-18)
        ])
        updateControls(loading:true)
    }

    @objc private func openFull() {
        full = true; snapshotLastEventID = nil; beforeCursors = [nil]; nextBeforeEventID = nil
        scope.stringValue = "Complete retained log"
        fullButton.isHidden = true
        loadPage()
    }
    @objc private func older() {
        guard full, let cursor = nextBeforeEventID else { return }
        beforeCursors.append(cursor); loadPage()
    }
    @objc private func newer() {
        guard full, beforeCursors.count > 1 else { return }
        beforeCursors.removeLast(); loadPage()
    }
    @objc private func copyPage() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(shownText,forType:.string)
    }
    private func updateControls(loading:Bool) {
        newerButton.isHidden = !full
        olderButton.isHidden = !full
        newerButton.isEnabled = !loading && beforeCursors.count > 1
        olderButton.isEnabled = !loading && nextBeforeEventID != nil
        copyButton.isEnabled = !loading && !shownText.isEmpty
    }
    private func loadPage(live: Bool = false) {
        generation += 1
        let request = generation
        let full = full, through = snapshotLastEventID, before = beforeCursors.last ?? nil
        if !live {
            count.stringValue = "Loading…"
            updateControls(loading:true)
        }
        let serviceID = service.id
        DispatchQueue.global(qos:.userInitiated).async { [hub, serviceID, weak self] in
            let result = Result { try hub.eventTracePage(service:serviceID,through:through,before:before,limit:full ? 50 : 10) }
            DispatchQueue.main.async { [weak self] in
                guard let self, self.generation == request else { return }
                switch result {
                case .success(let page): self.render(page)
                case .failure(let error):
                    self.count.stringValue = "Could not load events"
                    self.shownText = error.localizedDescription
                    self.textView.string = self.shownText
                    self.updateControls(loading:false)
                }
            }
        }
    }
    private func render(_ page: EventTracePage) {
        if full { snapshotLastEventID = page.snapshotLastEventID }
        nextBeforeEventID = page.nextBeforeEventID
        if page.traces.isEmpty {
            shownText = "No events recorded for this connection yet."
        } else {
            shownText = page.traces.map { trace in
                let eventTime = trace.stages.first.map { displayTraceTime($0.at) } ?? "Time unavailable"
                let version = trace.sopVersion.map { " · rules v\($0)" } ?? ""
                let direction:String
                switch trace.direction {
                case "outbound": direction = "Sent to \(trace.counterpartName ?? "Unknown connection")"
                case "inbound": direction = "Received from \(trace.counterpartName ?? "Unknown connection")"
                case "self": direction = "Direct message to this same connection"
                default:
                    direction = trace.eventID.hasPrefix("@dot:") ? "Received from Dot" :
                        trace.state.hasPrefix("dot_") ? "Sent to Dot" : "Local connection event"
                }
                let state = trace.deliveryMeaning ?? trace.state
                let requestPreview = trace.preview ?? (trace.wakeCheck == true ? "Session wake check" : "Message preview unavailable")
                let replyTime = trace.dotReplyAt.map(displayTraceTime) ?? "Reply time unavailable"
                if !full {
                    if trace.state == "dot_replied" {
                        let answer = trace.dotReplyPreview ?? "Dot reply preview unavailable"
                        return "\(answer)\n\(replyTime) · Dot replied\nRequest: \(requestPreview)"
                    }
                    let heading = trace.preview ?? (trace.wakeCheck == true ? "Session wake check" : trace.eventID)
                    return "\(heading)\n\(eventTime) · \(direction) · \(state)"
                }
                let sequence = trace.brokerSequence.map { "Broker sequence: \($0)\n" } ?? ""
                let reply = trace.inReplyTo.map { "In reply to event: \($0)\n" } ?? ""
                let stages = trace.stages.isEmpty ? "  No stage history recorded" : trace.stages.map {
                    let state:String
                    switch $0.state {
                    case "peer_delivered": state = "Queued for recipient"
                    case "peer_unconfirmed": state = "Delivery unconfirmed"
                    case "peer_received": state = "Received"
                    case "peer_acknowledged": state = "Acknowledged"
                    case "wake_pending" where trace.wakeCheck == true: state = "Wake check stored for recipient"
                    case "wake_verified" where trace.wakeCheck == true: state = "Authenticated connection acknowledged wake check · AI turn unconfirmed"
                    default: state = $0.state
                    }
                    return "  \(displayTraceTime($0.at))  \(state)  [\($0.basis)]"
                }.joined(separator:"\n")
                let preview = trace.preview ?? "Message preview unavailable"
                let eventLabel = trace.wakeCheck == true ? "Wake check" : "Event \(trace.eventID)"
                let counterpart = trace.counterpartID.map { "Counterpart ID: \($0)\n" } ?? ""
                let dotAnswer = trace.state == "dot_replied"
                    ? "Dot reply: \(trace.dotReplyPreview ?? "Preview unavailable")\nObserved (local): \(replyTime)\n\nRequest: \(preview)\n"
                    : "\(preview)\n"
                return "\(dotAnswer)\(direction) · \(eventTime)\n\n\(eventLabel)\(version)\n\(counterpart)\(sequence)\(reply)Current state: \(state)\nStages (local time):\n\(stages)"
            }.joined(separator:"\n\n────────────────────────────────────────\n\n")
        }
        if textView.string != shownText {
            textView.string = shownText
            textView.scrollRangeToVisible(NSRange(location:0,length:0))
        }
        if full {
            let newerCount = (beforeCursors.count-1)*50
            let first = page.traces.isEmpty ? 0 : page.total-newerCount-page.traces.count+1
            let last = page.traces.isEmpty ? 0 : page.total-newerCount
            count.stringValue = "Events \(first)–\(last) of \(page.total) · page \(beforeCursors.count)"
        } else {
            count.stringValue = "Newest \(page.traces.count) of \(page.total) retained events"
        }
        updateControls(loading:false)
    }
    func windowWillClose(_ notification: Notification) {
        NotificationCenter.default.removeObserver(self)
        generation += 1
        Self.openWindows.removeValue(forKey:service.id)
    }
}
