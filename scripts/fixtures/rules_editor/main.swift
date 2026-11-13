import AppKit
import DBondCore
import Foundation

@MainActor
private func require(_ condition: @autoclosure () -> Bool, _ detail: String) {
    guard condition() else { fatalError(detail) }
}

@MainActor
private func control(_ id: String, in view: NSView) -> NSView? {
    if view.identifier?.rawValue == id { return view }
    for child in view.subviews {
        if let found = control(id, in: child) { return found }
    }
    return nil
}

@MainActor
private func tagged<T: NSView>(_ id: String, _ window: NSWindow, as type: T.Type) -> T {
    guard let root = window.contentView, let found = control(id, in: root) as? T else {
        fatalError("Missing live \(T.self) control \(id)")
    }
    return found
}

@MainActor
private func changed(_ editor: NSTextView, to text: String) {
    editor.string = text
    editor.delegate?.textDidChange?(Notification(name: NSText.didChangeNotification, object: editor))
}

@MainActor
private func action(_ button: NSButton) {
    require(button.isEnabled, "Disabled button \(button.identifier?.rawValue ?? "unknown")")
    button.performClick(nil)
}

@MainActor
private func screenshot(_ window: NSWindow, name: String) {
    guard let path = ProcessInfo.processInfo.environment["RULES_EDITOR_QA_DIR"] else { return }
    let folder = URL(fileURLWithPath: path, isDirectory: true)
    let bounds = window.contentView!.bounds
    guard let bitmap = window.contentView!.bitmapImageRepForCachingDisplay(in: bounds) else { return }
    window.contentView!.cacheDisplay(in: bounds, to: bitmap)
    guard let png = bitmap.representation(using: .png, properties: [:]) else { return }
    try! png.write(to: folder.appendingPathComponent(name + ".png"), options: .atomic)
    print("QA image: \(folder.appendingPathComponent(name + ".png").path)")
}

@main
struct RulesEditorFixture {
    @MainActor static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let mixed = "# Original **heading**\n\n- [x] checked\n1. ordered `code`\n> quoted\n```swift\nlet literal = \"<script>\"\n```\n**bold** and <script>literal</script>\n"
        let first = OwnerRule(id: "stable-first", text: mixed)
        let second = OwnerRule(id: "stable-second", text: "## Second\r\nKeep my ID and content.\r\n")
        var stored = OwnerSOP(version: 7, rules: [first, second])
        var saves: [(Int, OwnerSOP)] = []
        let ui = RuleEditor(serviceName: "Fixture / Rules", initial: stored,
            load: { stored }, persist: { next, expected in
                guard stored.version == expected else { throw NSError(domain: "Fixture", code: 409, userInfo: [NSLocalizedDescriptionKey: "Owner SOP changed; reload before saving."]) }
                saves.append((expected, next)); stored = next
            })
        ui.show()
        ui.window.displayIfNeeded()
        let window = ui.window
        let list = tagged("rules.list", window, as: NSTableView.self)
        let editor = tagged("rules.editor", window, as: NSTextView.self)
        let preview = tagged("rules.preview", window, as: NSTextView.self)
        let mode = tagged("rules.mode", window, as: NSSegmentedControl.self)
        let add = tagged("rules.add", window, as: NSButton.self)
        let remove = tagged("rules.delete", window, as: NSButton.self)
        let up = tagged("rules.up", window, as: NSButton.self)
        let down = tagged("rules.down", window, as: NSButton.self)
        let save = tagged("rules.save", window, as: NSButton.self)
        let status = tagged("rules.status", window, as: NSTextField.self)
        let heading = tagged("rules.format.heading", window, as: NSButton.self)
        let bold = tagged("rules.format.bold", window, as: NSButton.self)
        let bullet = tagged("rules.format.list", window, as: NSButton.self)
        let task = tagged("rules.format.task", window, as: NSButton.self)
        let code = tagged("rules.format.code", window, as: NSButton.self)
        require(list.numberOfRows == 2 && list.selectedRow == 0, "Initial list/selection")
        require(editor.string == mixed, "Initial Markdown source changed")
        require(!save.isEnabled, "Clean document save enabled")
        mode.selectedSegment = 1
        _ = mode.sendAction(mode.action, to: mode.target)
        require(!preview.isEditable && preview.string.contains("<script>literal</script>"), "Preview interpreted raw HTML")
        require(editor.string == mixed, "Preview rewrote Markdown source")
        screenshot(window, name: "rules-preview")
        mode.selectedSegment = 0
        _ = mode.sendAction(mode.action, to: mode.target)
        require(editor.string == mixed, "Mode switch rewrote Markdown source")

        action(add)
        require(list.numberOfRows == 3 && editor.string.isEmpty && !save.isEnabled, "Add/empty rule validation")
        action(heading)
        require(editor.string == "## ", "Heading insertion")
        changed(editor, to: "alpha\r\nbeta")
        editor.setSelectedRange(NSRange(location: 0, length: (editor.string as NSString).length))
        action(bullet)
        require(editor.string == "- alpha\r\n- beta", "Bullet formatting changed CRLF or missed a line")
        changed(editor, to: "alpha\r\nbeta")
        editor.setSelectedRange(NSRange(location: 0, length: (editor.string as NSString).length))
        action(task)
        require(editor.string == "- [ ] alpha\r\n- [ ] beta", "Checklist formatting changed CRLF or missed a line")
        changed(editor, to: "alpha\r\nbeta")
        editor.setSelectedRange(NSRange(location: 0, length: 5))
        action(bold)
        require(editor.string == "**alpha**\r\nbeta", "Bold formatting changed CRLF or selection")
        changed(editor, to: "alpha\r\nbeta")
        editor.setSelectedRange(NSRange(location: 0, length: 5))
        action(code)
        require(editor.string == "`alpha`\r\nbeta", "Code formatting changed CRLF or selection")
        changed(editor, to: "### New rule\n- use **Markdown**\n")
        require(save.isEnabled, "Filled rule did not enable save")
        action(up)
        require(list.selectedRow == 1, "Move up did not select moved rule")
        action(down)
        require(list.selectedRow == 2, "Move down did not select moved rule")
        action(up)
        action(save)
        require(stored.version == 8 && saves.count == 1 && saves[0].0 == 7, "CAS save/version")
        require(stored.rules.map(\.id) == ["stable-first", stored.rules[1].id, "stable-second"], "Order or stable IDs")
        require(stored.rules[0].text == mixed, "Existing Markdown bytes changed on reorder/save")
        require(stored.rules[2].text == second.text, "Existing CRLF source changed on reorder/save")
        require(!save.isEnabled, "Saved document remained dirty")
        screenshot(window, name: "rules-saved")

        action(remove)
        require(list.numberOfRows == 2 && save.isEnabled, "Delete did not dirty draft")
        action(save)
        require(stored.version == 9 && stored.rules.map(\.id) == ["stable-first", "stable-second"], "Delete persisted wrong rules")

        changed(editor, to: "# My unsaved edit\n" + editor.string)
        require(save.isEnabled, "Edit did not dirty draft")
        stored = OwnerSOP(version: 10, rules: stored.rules + [.init(id: "external", text: "External current")])
        action(save)
        require(stored.version == 10 && saves.count == 2, "Stale editor overwrote external save")
        require(!save.isEnabled && status.stringValue.contains("changed elsewhere"), "Revision conflict did not disable save")
        require(editor.string.hasPrefix("# My unsaved edit\n"), "Conflict discarded owner's draft")
        require(list.numberOfRows == 2, "Conflict replaced draft with external current")
        screenshot(window, name: "rules-conflict")
        print("PASS: native rules list, Markdown source/preview/formatting/CRLF, add/reorder/delete, blank guard, CAS conflict and retained draft")
    }
}
