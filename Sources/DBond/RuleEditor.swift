import AppKit
import DBondCore

// All changes stay in this window until the owner saves one version of the ordered rules.
@MainActor
final class RuleEditor: NSObject, NSWindowDelegate, NSTableViewDataSource, NSTableViewDelegate, NSTextViewDelegate {
    let window: NSWindow
    var onClose: (() -> Void)?
    private let load: () throws -> OwnerSOP?
    private let persist: (OwnerSOP, Int) throws -> Void
    private var baseline: [OwnerRule]
    private var version: Int
    private var draft: [OwnerRule]
    private var active: Int?
    private var changing = false
    private var conflict = false

    private let table = NSTableView()
    private let editor = RuleTextView()
    private let preview = NSTextView()
    private let sourceScroll = NSScrollView(), previewScroll = NSScrollView()
    private let formatting = NSStackView()
    private let copyDraft = NSButton()
    private let mode = NSSegmentedControl(labels: ["Write", "Preview"], trackingMode: .selectOne, target: nil, action: nil)
    private let title = NSTextField(labelWithString: "")
    private let count = NSTextField(labelWithString: "")
    private let status = NSTextField(labelWithString: "")
    private let empty = NSTextField(wrappingLabelWithString: "Add your first rule to start writing.")
    private let add = NSButton(), remove = NSButton(), up = NSButton(), down = NSButton(), save = NSButton(), reload = NSButton()

    init(serviceName: String, initial: OwnerSOP?, load: @escaping () throws -> OwnerSOP?, persist: @escaping (OwnerSOP, Int) throws -> Void) {
        self.load = load; self.persist = persist
        baseline = initial?.rules ?? []; draft = baseline; version = initial?.version ?? 0
        window = NSWindow(contentRect: NSRect(x:0,y:0,width:880,height:560), styleMask:[.titled,.closable,.miniaturizable,.resizable], backing:.buffered, defer:false)
        super.init()
        window.isReleasedWhenClosed = false; window.title = "Rules · " + serviceName
        window.minSize = NSSize(width:700,height:410); window.appearance = NSAppearance(named:.darkAqua)
        window.delegate = self; window.center()
        build(serviceName)
        select(draft.isEmpty ? nil : 0)
    }

    func show() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps:true) }

    private func button(_ b: NSButton, _ text: String, _ id: String, _ action: Selector, symbol: String? = nil) {
        b.identifier = .init("rules." + id); b.title = text; b.bezelStyle = .rounded
        b.target = self; b.action = action
        if let symbol {
            b.image = NSImage(systemSymbolName:symbol, accessibilityDescription:nil)
            b.imagePosition = text.isEmpty ? .imageOnly : .imageLeading
        }
    }

    private func build(_ service: String) {
        let root = NSView()
        window.contentView = root
        let icon = NSImageView(); icon.image = Brand.app; icon.imageScaling = .scaleProportionallyUpOrDown
        let heading = NSTextField(labelWithString: "Connection rules")
        heading.font = .systemFont(ofSize:23,weight:.semibold)
        let subtitle = NSTextField(labelWithString: service); subtitle.textColor = .secondaryLabelColor
        let header = NSStackView(views:[heading, subtitle]); header.orientation = .vertical; header.alignment = .leading; header.spacing = 2
        let separator = NSBox(); separator.boxType = .separator
        let divider = NSBox(); divider.boxType = .separator
        let foot = NSBox(); foot.boxType = .separator
        count.font = .systemFont(ofSize:12,weight:.semibold); count.textColor = .secondaryLabelColor
        title.font = .systemFont(ofSize:16,weight:.semibold)
        title.lineBreakMode = .byTruncatingTail; title.setContentCompressionResistancePriority(.defaultLow, for:.horizontal)
        let listScroll = NSScrollView(); listScroll.hasVerticalScroller = true; listScroll.autohidesScrollers = true
        listScroll.borderType = .noBorder; listScroll.drawsBackground = false
        table.identifier = .init("rules.list"); table.setAccessibilityLabel("Rules in reading order")
        table.headerView = nil; table.rowHeight = 35; table.intercellSpacing = .init(width:0,height:3)
        table.style = .sourceList; table.usesAlternatingRowBackgroundColors = false
        table.allowsEmptySelection = false; table.allowsMultipleSelection = false
        let column = NSTableColumn(identifier:.init("rule")); column.resizingMask = .autoresizingMask
        table.addTableColumn(column); table.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
        table.delegate = self; table.dataSource = self; listScroll.documentView = table
        button(add,"Add","add",#selector(addRule),symbol:"plus")
        button(remove,"","delete",#selector(deleteRule),symbol:"trash")
        button(up,"","up",#selector(moveUp),symbol:"chevron.up")
        button(down,"","down",#selector(moveDown),symbol:"chevron.down")
        for (control, text) in [(add,"Add rule"),(remove,"Remove selected rule"),(up,"Move rule up"),(down,"Move rule down")] {
            control.toolTip = text; control.setAccessibilityLabel(text); control.controlSize = .small
        }
        let actions = NSStackView(views:[add,up,down,remove]); actions.spacing = 4
        mode.identifier = .init("rules.mode"); mode.selectedSegment = 0; mode.target = self; mode.action = #selector(switchMode)
        mode.setAccessibilityLabel("Markdown editor mode")
        for (text, scroll, isSource) in [(editor as NSTextView,sourceScroll,true),(preview,previewScroll,false)] {
            text.isEditable = isSource; text.isSelectable = true; text.isRichText = !isSource
            text.identifier = .init(isSource ? "rules.editor" : "rules.preview")
            text.setAccessibilityLabel(isSource ? "Rule Markdown source" : "Formatted rule preview")
            text.textContainerInset = .init(width:16,height:16)
            text.font = .monospacedSystemFont(ofSize:14,weight:.regular); text.textColor = .textColor
            text.drawsBackground = false; text.isVerticallyResizable = true; text.isHorizontallyResizable = false
            text.autoresizingMask = [.width]; text.maxSize = .init(width:CGFloat.greatestFiniteMagnitude,height:CGFloat.greatestFiniteMagnitude)
            text.textContainer?.widthTracksTextView = true
            text.textContainer?.containerSize = .init(width:0,height:CGFloat.greatestFiniteMagnitude)
            text.isAutomaticLinkDetectionEnabled = false; text.isAutomaticDataDetectionEnabled = false
            scroll.borderType = .bezelBorder; scroll.drawsBackground = true; scroll.backgroundColor = .textBackgroundColor
            scroll.hasVerticalScroller = true; scroll.autohidesScrollers = true; scroll.documentView = text
        }
        for (tag, title, help, icon) in [(0,"Heading","Add a Markdown heading","textformat.size.larger"),(1,"Bold","Bold the selection","bold"),(2,"List","Add a bulleted list","list.bullet"),(3,"Task","Add a checklist","checklist"),(4,"Code","Format inline code","chevron.left.forwardslash.chevron.right")] {
            let tool = NSButton()
            button(tool, "", "format." + title.lowercased(), #selector(formatText(_:)), symbol:icon)
            tool.tag = tag; tool.controlSize = .small; tool.toolTip = help; tool.setAccessibilityLabel(help)
            formatting.addArrangedSubview(tool)
        }
        formatting.spacing = 4
        editor.delegate = self; editor.allowsUndo = true; editor.importsGraphics = false
        editor.isAutomaticQuoteSubstitutionEnabled = false; editor.isAutomaticDashSubstitutionEnabled = false
        editor.isAutomaticTextReplacementEnabled = false; editor.isAutomaticSpellingCorrectionEnabled = false
        editor.isAutomaticTextCompletionEnabled = false; editor.smartInsertDeleteEnabled = false
        empty.textColor = .secondaryLabelColor; empty.alignment = .center
        button(save,"Save rules","save",#selector(saveRules)); save.keyEquivalent = "s"; save.keyEquivalentModifierMask = [.command]
        button(reload,"Reload saved rules…","reload",#selector(reloadRules)); reload.isHidden = true
        status.identifier = .init("rules.status"); status.font = .systemFont(ofSize:12)
        status.textColor = .secondaryLabelColor; status.lineBreakMode = .byTruncatingTail
        button(copyDraft,"Copy draft","copy",#selector(copyAll)); copyDraft.isHidden = true
        let footer = NSStackView(views:[copyDraft,reload,save]); footer.spacing = 8

        for view in [icon,header,separator,divider,foot,count,listScroll,actions,title,mode,formatting,sourceScroll,previewScroll,empty,status,footer] {
            view.translatesAutoresizingMaskIntoConstraints = false; root.addSubview(view)
        }
        NSLayoutConstraint.activate([
            icon.leadingAnchor.constraint(equalTo:root.leadingAnchor,constant:18), icon.topAnchor.constraint(equalTo:root.topAnchor,constant:12), icon.widthAnchor.constraint(equalToConstant:48), icon.heightAnchor.constraint(equalToConstant:48),
            header.leadingAnchor.constraint(equalTo:icon.trailingAnchor,constant:10), header.centerYAnchor.constraint(equalTo:icon.centerYAnchor), header.trailingAnchor.constraint(lessThanOrEqualTo:root.trailingAnchor,constant:-18),
            separator.topAnchor.constraint(equalTo:icon.bottomAnchor,constant:12), separator.leadingAnchor.constraint(equalTo:root.leadingAnchor),separator.trailingAnchor.constraint(equalTo:root.trailingAnchor),
            divider.leadingAnchor.constraint(equalTo:root.leadingAnchor,constant:224), divider.widthAnchor.constraint(equalToConstant:1), divider.topAnchor.constraint(equalTo:separator.bottomAnchor), divider.bottomAnchor.constraint(equalTo:foot.topAnchor),
            count.leadingAnchor.constraint(equalTo:root.leadingAnchor,constant:18), count.centerYAnchor.constraint(equalTo:mode.centerYAnchor),
            listScroll.topAnchor.constraint(equalTo:mode.bottomAnchor,constant:10), listScroll.leadingAnchor.constraint(equalTo:root.leadingAnchor,constant:6), listScroll.trailingAnchor.constraint(equalTo:divider.leadingAnchor,constant:-6), listScroll.bottomAnchor.constraint(equalTo:actions.topAnchor,constant:-8),
            actions.leadingAnchor.constraint(equalTo:root.leadingAnchor,constant:14), actions.bottomAnchor.constraint(equalTo:foot.topAnchor,constant:-12),
            mode.topAnchor.constraint(equalTo:separator.bottomAnchor,constant:12), mode.trailingAnchor.constraint(equalTo:root.trailingAnchor,constant:-18),
            title.leadingAnchor.constraint(equalTo:divider.trailingAnchor,constant:17), title.centerYAnchor.constraint(equalTo:mode.centerYAnchor), title.trailingAnchor.constraint(lessThanOrEqualTo:mode.leadingAnchor,constant:-8),
            formatting.leadingAnchor.constraint(equalTo:title.leadingAnchor), formatting.topAnchor.constraint(equalTo:mode.bottomAnchor,constant:6),
            sourceScroll.leadingAnchor.constraint(equalTo:title.leadingAnchor), sourceScroll.topAnchor.constraint(equalTo:mode.bottomAnchor,constant:36), sourceScroll.trailingAnchor.constraint(equalTo:mode.trailingAnchor), sourceScroll.bottomAnchor.constraint(equalTo:foot.topAnchor,constant:-12),
            previewScroll.leadingAnchor.constraint(equalTo:sourceScroll.leadingAnchor), previewScroll.trailingAnchor.constraint(equalTo:sourceScroll.trailingAnchor), previewScroll.topAnchor.constraint(equalTo:mode.bottomAnchor,constant:10), previewScroll.bottomAnchor.constraint(equalTo:sourceScroll.bottomAnchor),
            empty.centerXAnchor.constraint(equalTo:sourceScroll.centerXAnchor), empty.centerYAnchor.constraint(equalTo:sourceScroll.centerYAnchor),
            foot.leadingAnchor.constraint(equalTo:root.leadingAnchor), foot.trailingAnchor.constraint(equalTo:root.trailingAnchor), foot.bottomAnchor.constraint(equalTo:root.bottomAnchor,constant:-54),
            footer.trailingAnchor.constraint(equalTo:root.trailingAnchor,constant:-18), footer.centerYAnchor.constraint(equalTo:foot.bottomAnchor,constant:27),
            status.leadingAnchor.constraint(equalTo:root.leadingAnchor,constant:18),status.centerYAnchor.constraint(equalTo:footer.centerYAnchor),status.trailingAnchor.constraint(lessThanOrEqualTo:footer.leadingAnchor,constant:-10)
        ])
        window.initialFirstResponder = table
    }

    private func name(_ rule: OwnerRule) -> String {
        let lines = rule.text.split(whereSeparator: \.isNewline).map { $0.trimmingCharacters(in:.whitespacesAndNewlines) }
        if let heading = lines.first(where: { $0.hasPrefix("#") }) {
            let label = String(heading.drop(while: {$0 == "#"})).trimmingCharacters(in:.whitespaces)
            if !label.isEmpty { return String(label.prefix(160)) }
        }
        for line in lines {
            if line.isEmpty || line.lowercased().hasPrefix("enabled:") { continue }
            let title = String(line.drop(while: {$0 == "#"})).trimmingCharacters(in:.whitespaces)
            if !title.isEmpty { return String(title.prefix(160)) }
        }
        return "Untitled rule"
    }

    func numberOfRows(in tableView: NSTableView) -> Int { draft.count }
    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard draft.indices.contains(row) else { return nil }
        let cell = NSTableCellView(); let text = NSTextField(labelWithString:"\(row + 1).  " + name(draft[row]))
        text.lineBreakMode = .byTruncatingTail; text.font = .systemFont(ofSize:13,weight:.medium)
        text.translatesAutoresizingMaskIntoConstraints = false; cell.addSubview(text); cell.textField = text
        NSLayoutConstraint.activate([text.leadingAnchor.constraint(equalTo:cell.leadingAnchor,constant:5),text.trailingAnchor.constraint(equalTo:cell.trailingAnchor,constant:-4),text.centerYAnchor.constraint(equalTo:cell.centerYAnchor)])
        cell.toolTip = "\(name(draft[row])) · \(draft[row].id)"; return cell
    }

    func tableViewSelectionDidChange(_ notification: Notification) {
        guard !changing, draft.indices.contains(table.selectedRow), table.selectedRow != active else { return }
        select(table.selectedRow, reloadTable:false)
    }

    private func select(_ row: Int?, reloadTable: Bool = true) {
        changing = true; active = row
        if reloadTable { table.reloadData() }
        if let row { table.selectRowIndexes(IndexSet(integer:row),byExtendingSelection:false); table.scrollRowToVisible(row) }
        editor.undoManager?.removeAllActions(withTarget:editor)
        // NSTextView undo operations can target its text storage; don't carry them between rules.
        window.undoManager?.removeAllActions()
        editor.string = row.map { draft[$0].text } ?? ""
        editor.setSelectedRange(.init(location:0,length:0)); editor.scrollRangeToVisible(.init(location:0,length:0))
        changing = false; refreshMode(); refresh()
    }

    func textDidChange(_ notification: Notification) {
        guard !changing, let i = active else { return }
        draft[i].text = editor.string
        if let cell = table.view(atColumn:0,row:i,makeIfNecessary:false) as? NSTableCellView {
            let heading = name(draft[i]); cell.textField?.stringValue = "\(i + 1).  " + heading; cell.toolTip = "\(heading) · \(draft[i].id)"
        }
        refresh()
    }

    private func refreshMode() {
        let reading = mode.selectedSegment == 1
        formatting.isHidden = reading || active == nil
        sourceScroll.isHidden = active == nil || reading; previewScroll.isHidden = active == nil || !reading
        empty.isHidden = active != nil; mode.isEnabled = active != nil
        if reading, let i = active {
            preview.textStorage?.setAttributedString(RuleMarkdown.preview(draft[i].text))
            preview.scrollRangeToVisible(.init(location:0,length:0))
        }
    }
    @objc private func switchMode() { refreshMode() }

    @objc private func formatText(_ control: NSButton) {
        guard active != nil, mode.selectedSegment == 0 else { return }
        let source = editor.string as NSString, selected = editor.selectedRange()
        var range = selected
        var replacement: String
        let inner: NSRange
        if control.tag == 1 || control.tag == 4 {
            let wrap = control.tag == 1 ? "**" : "`"
            let word = selected.length > 0 ? source.substring(with:selected) : (control.tag == 1 ? "bold text" : "code")
            replacement = wrap + word + wrap
            inner = NSRange(location:range.location+(wrap as NSString).length,length:(word as NSString).length)
        } else {
            range = source.lineRange(for:selected)
            let original = source.substring(with:range)
            let prefix = control.tag == 0 ? "## " : (control.tag == 2 ? "- " : "- [ ] ")
            // Match individual line starts without touching LF, CRLF or paragraph separators.
            let mutable = NSMutableString(string:original)
            let lines = original as NSString
            if lines.length == 0 { mutable.append(prefix) }
            else {
                var starts = [Int](), position = 0
                while position < lines.length {
                    starts.append(position)
                    position = NSMaxRange(lines.lineRange(for:NSRange(location:position,length:0)))
                }
                for index in starts.reversed() { mutable.insert(prefix,at:index) }
            }
            replacement = mutable as String
            inner = NSRange(location:selected.location + (prefix as NSString).length, length:0)
        }
        guard editor.shouldChangeText(in:range,replacementString:replacement) else { return }
        editor.textStorage?.replaceCharacters(in:range,with:replacement); editor.didChangeText()
        editor.setSelectedRange(inner); window.makeFirstResponder(editor)
    }

    @objc private func copyAll() {
        let value = draft.map(\.text).joined(separator:"\n\n---\n\n")
        NSPasteboard.general.clearContents(); NSPasteboard.general.setString(value,forType:.string)
    }

    private func refresh() {
        let dirty = draft != baseline
        window.isDocumentEdited = dirty
        title.stringValue = active.map { name(draft[$0]) } ?? "Markdown"
        count.stringValue = draft.count == 1 ? "1 rule" : "\(draft.count) rules"
        add.isEnabled = draft.count < 64; remove.isEnabled = active != nil
        up.isEnabled = (active ?? 0) > 0; down.isEnabled = active.map { $0 + 1 < draft.count } ?? false
        let blank = draft.firstIndex { $0.text.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty }
        let bytes = draft.reduce(0) { $0 + $1.id.utf8.count + $1.text.utf8.count }
        let issue: String? = blank.map { "Rule \($0 + 1) is empty. Write instructions or remove it." } ?? (bytes > 131072 ? "Rules exceed 128 KiB. Shorten the text before saving." : nil)
        save.isEnabled = dirty && issue == nil && !conflict
        status.textColor = (conflict || issue != nil) ? .systemOrange : .secondaryLabelColor
        status.stringValue = conflict ? "Saved rules changed elsewhere. Your draft is still here." : issue ?? (dirty ? "Unsaved changes · applies to new events after saving" : version > 0 ? "Saved version \(version) · used for new events" : "No saved rules yet")
        status.toolTip = status.stringValue
        reload.isHidden = !conflict; copyDraft.isHidden = !conflict
    }

    @objc private func addRule() {
        guard draft.count < 64 else { return }
        draft.append(.init(id:UUID().uuidString,text:"")); mode.selectedSegment = 0
        select(draft.count-1); window.makeFirstResponder(editor)
    }
    @objc private func deleteRule() {
        guard let i = active else { return }
        draft.remove(at:i); select(draft.isEmpty ? nil : min(i,draft.count-1))
    }
    @objc private func moveUp() { move(-1) }
    @objc private func moveDown() { move(1) }
    private func move(_ step: Int) {
        guard let i = active, draft.indices.contains(i+step) else { return }
        draft.swapAt(i,i+step); select(i+step)
    }

    @objc private func saveRules() {
        guard save.isEnabled else { return }
        let next = OwnerSOP(version:version+1,rules:draft)
        do { try persist(next,version); version = next.version; baseline = draft; refresh() }
        catch {
            do { conflict = (try load()?.version ?? 0) != version } catch { conflict = false }
            refresh()
            if !conflict {
                status.stringValue = "Could not save: " + error.localizedDescription; status.toolTip = status.stringValue; status.textColor = .systemOrange
            }
        }
    }

    @objc private func reloadRules() {
        guard confirmDiscard(title:"Replace your draft with the saved rules?", action:"Reload saved rules") else { return }
        do {
            let current = try load(); baseline = current?.rules ?? []; draft = baseline
            version = current?.version ?? 0; conflict = false; select(draft.isEmpty ? nil : 0)
        } catch { status.stringValue = error.localizedDescription; status.toolTip = status.stringValue }
    }
    private func confirmDiscard(title: String, action: String) -> Bool {
        guard draft != baseline else { return true }
        let alert = Brand.alert(); alert.messageText = title; alert.informativeText = "Your unsaved edits to this connection will be discarded."
        alert.addButton(withTitle:"Keep editing"); alert.addButton(withTitle:action)
        return alert.runModal() == .alertSecondButtonReturn
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool { confirmDiscard(title:"Close without saving your rules?",action:"Discard changes") }
    func windowWillClose(_ notification: Notification) { onClose?() }
}

// The menu bar app does not provide a main Edit menu. Keep normal Mac editing shortcuts here.
private final class RuleTextView: NSTextView {
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        let mods = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        guard mods == [.command] || mods == [.command,.shift] else { return super.performKeyEquivalent(with:event) }
        switch (event.charactersIgnoringModifiers?.lowercased(), mods.contains(.shift)) {
        case ("a",false): selectAll(nil)
        case ("c",false): copy(nil)
        case ("v",false): pasteAsPlainText(nil)
        case ("x",false): cut(nil)
        case ("z",false): undoManager?.undo()
        case ("z",true): undoManager?.redo()
        default: return super.performKeyEquivalent(with:event)
        }
        return true
    }
}
