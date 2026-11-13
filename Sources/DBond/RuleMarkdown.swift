import AppKit

/// A local, read-only rendering of rule Markdown. It never interprets HTML or URLs.
@MainActor
enum RuleMarkdown {
    static func preview(_ source: String) -> NSAttributedString {
        let result = NSMutableAttributedString(string: "")
        let lines = source.split(separator: "\n", omittingEmptySubsequences: false)
        var fence: Character?

        for (index, slice) in lines.enumerated() {
            let line = String(slice)
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if let marker = fence {
                if isFence(trimmed, marker: marker) { fence = nil }
                else { appendLine(line, kind: .codeBlock, to: result) }
                continue
            }
            if let marker = trimmed.first, (marker == "`" || marker == "~"), isFence(trimmed, marker: marker) {
                fence = marker
                continue
            }
            if trimmed.isEmpty {
                // A single blank paragraph separates blocks without expanding every soft break.
                if index < lines.count - 1 { result.append(NSAttributedString(string: "\n")) }
                continue
            }
            if let (depth, text) = heading(line) {
                appendLine(text, kind: .heading(depth), to: result)
            } else if let quote = quote(line) {
                appendLine(quote, kind: .quote, to: result)
            } else if let list = listItem(line) {
                appendLine(list.text, kind: .list(list.prefix, list.indent), to: result)
            } else {
                appendLine(line, kind: .paragraph, to: result)
            }
        }
        return result
    }

    private enum Kind {
        case paragraph, heading(Int), quote, list(String, Int), codeBlock
    }

    private static func isFence(_ text: String, marker: Character) -> Bool {
        var count = 0
        for char in text {
            if char == marker { count += 1 } else { break }
        }
        return count >= 3
    }

    private static func heading(_ line: String) -> (Int, String)? {
        let chars = Array(line)
        var depth = 0
        while depth < chars.count && depth < 6 && chars[depth] == "#" { depth += 1 }
        guard depth > 0, depth < chars.count, chars[depth].isWhitespace else { return nil }
        return (depth, String(chars.dropFirst(depth + 1)))
    }

    private static func quote(_ line: String) -> String? {
        let text = line.drop(while: { $0 == " " || $0 == "\t" })
        guard text.first == ">" else { return nil }
        var body = text.dropFirst()
        if body.first == " " { body = body.dropFirst() }
        return String(body)
    }

    private static func listItem(_ line: String) -> (prefix: String, text: String, indent: Int)? {
        let chars = Array(line)
        var i = 0
        while i < chars.count && (chars[i] == " " || chars[i] == "\t") { i += 1 }
        let indent = min(i, 24)
        guard i < chars.count else { return nil }
        if ["-", "*", "+"].contains(chars[i]), i + 1 < chars.count, chars[i + 1].isWhitespace {
            let start = i + 2
            if start + 3 < chars.count, chars[start] == "[", (chars[start + 1] == " " || chars[start + 1] == "x" || chars[start + 1] == "X"), chars[start + 2] == "]", chars[start + 3].isWhitespace {
                let checked = chars[start + 1] != " "
                return (checked ? "☑ " : "☐ ", String(chars.dropFirst(start + 4)), indent)
            }
            return ("• ", String(chars.dropFirst(start)), indent)
        }
        let numberStart = i
        while i < chars.count && chars[i].isNumber && i - numberStart < 8 { i += 1 }
        guard i > numberStart, i + 1 < chars.count, (chars[i] == "." || chars[i] == ")"), chars[i + 1].isWhitespace else { return nil }
        return (String(chars[numberStart...i]) + " ", String(chars.dropFirst(i + 2)), indent)
    }

    private static func appendLine(_ text: String, kind: Kind, to result: NSMutableAttributedString) {
        let style = NSMutableParagraphStyle()
        style.lineSpacing = 3
        style.paragraphSpacing = 5
        var font = NSFont.systemFont(ofSize: 13)
        var color = NSColor.labelColor
        var prefix = ""
        var background: NSColor?
        var inline = true

        switch kind {
        case .paragraph: break
        case .heading(let depth):
            let sizes: [CGFloat] = [25, 22, 19, 17, 15, 14]
            font = .systemFont(ofSize: sizes[depth - 1], weight: .semibold)
            style.paragraphSpacing = depth < 3 ? 10 : 7
        case .quote:
            prefix = "▍  "
            color = .secondaryLabelColor
            style.headIndent = 19
            style.firstLineHeadIndent = 0
        case .list(let marker, let indent):
            prefix = marker
            let offset = CGFloat(indent) * 7
            style.firstLineHeadIndent = offset
            style.headIndent = offset + 22
        case .codeBlock:
            font = .monospacedSystemFont(ofSize: 12, weight: .regular)
            color = .secondaryLabelColor
            background = .controlBackgroundColor
            style.headIndent = 12
            style.firstLineHeadIndent = 12
            style.paragraphSpacing = 0
            inline = false
        }

        var attributes: [NSAttributedString.Key: Any] = [
            .font: font, .foregroundColor: color, .paragraphStyle: style
        ]
        if let background { attributes[.backgroundColor] = background }
        if !prefix.isEmpty {
            var prefixAttributes = attributes
            prefixAttributes[.foregroundColor] = NSColor.controlAccentColor
            result.append(NSAttributedString(string: prefix, attributes: prefixAttributes))
        }
        if inline { appendInline(text, base: attributes, to: result) }
        else { result.append(NSAttributedString(string: "  " + text + "  ", attributes: attributes)) }
        result.append(NSAttributedString(string: "\n", attributes: attributes))
    }

    private static func appendInline(_ text: String, base: [NSAttributedString.Key: Any], to result: NSMutableAttributedString) {
        let chars = Array(text)
        var i = 0
        var plain = 0
        func flush(_ end: Int) {
            if plain < end { result.append(NSAttributedString(string: String(chars[plain..<end]), attributes: base)) }
        }
        while i < chars.count {
            let code = chars[i] == "`"
            let bold = i + 1 < chars.count && (chars[i] == "*" || chars[i] == "_") && chars[i + 1] == chars[i]
            guard code || bold else { i += 1; continue }
            let width = code ? 1 : 2
            var close = i + width
            while close + width <= chars.count {
                if chars[close] == chars[i] && (width == 1 || chars[close + 1] == chars[i]) { break }
                close += 1
            }
            guard close + width <= chars.count, close > i + width else { i += width; continue }
            flush(i)
            var attributes = base
            if code {
                attributes[.font] = NSFont.monospacedSystemFont(ofSize: 12, weight: .medium)
                attributes[.backgroundColor] = NSColor.controlBackgroundColor
            } else {
                let size = (base[.font] as? NSFont)?.pointSize ?? 13
                attributes[.font] = NSFont.systemFont(ofSize: size, weight: .bold)
            }
            result.append(NSAttributedString(string: String(chars[(i + width)..<close]), attributes: attributes))
            i = close + width
            plain = i
        }
        flush(chars.count)
    }
}
