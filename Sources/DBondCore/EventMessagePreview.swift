import Foundation

enum EventMessagePreview {
    static let limit = 210

    static func make(_ data: Data, eventID: String, service: String) -> String? {
        if service == "owner" && eventID.hasPrefix("@wake:") { return "Session wake check" }
        guard !data.isEmpty else { return nil }
        let raw: String?
        if let decoded = try? JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed]) {
            guard let object = decoded as? [String: Any] else {
                return (decoded as? String).flatMap(summarize)
            }
            let kind = (object["kind"] as? String ?? "").lowercased()
            if kind.contains("policy") || kind.contains("sop") || kind.contains("token") { return nil }
            // These are display fields, never a serialized envelope or transport metadata.
            let job = object["job"] as? [String: Any]
            let heading = (job?["title"] as? String) ?? (object["title"] as? String)
            let message = (object["text"] as? String) ?? (object["message"] as? String)
            raw = message ?? heading ?? (object["body"] as? String)
        } else {
            raw = String(data: data, encoding: .utf8)
        }
        guard let raw else { return nil }
        return summarize(raw)
    }





    static func summarize(_ raw: String) -> String? {
        var source = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !source.isEmpty else { return nil }
        if source.hasPrefix("```") {
            let lines = source.components(separatedBy: .newlines)
            let language = String(lines[0].dropFirst(3)).trimmingCharacters(in: .whitespaces)
            let snippet = lines.dropFirst().first(where: { !$0.trimmingCharacters(in: .whitespaces).isEmpty && !$0.hasPrefix("```") }) ?? ""
            source = (language.isEmpty ? "Code" : "Code (\(language))") + ": " + snippet
        } else if let fence = source.range(of: "```") {
            // Leading prose is the human summary; code remains in the original body.
            source = String(source[..<fence.lowerBound])
        }
        let collapsed = source.replacingOccurrences(of: #"@wake:[^\s]+"#, with: "[wake check]", options: .regularExpression)
            .replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !collapsed.isEmpty else { return nil }
        let firstTwo = collapsed.range(of: #"^.*?[.!?](?:\s+.*?[.!?])?(?=\s|$)"#, options: .regularExpression)
            .map { String(collapsed[$0]) } ?? collapsed
        let candidate = firstTwo
        if candidate.count <= limit { return candidate }
        let end = candidate.index(candidate.startIndex, offsetBy: limit - 1)
        return String(candidate[..<end]).trimmingCharacters(in: .whitespacesAndNewlines) + "…"
    }
}
