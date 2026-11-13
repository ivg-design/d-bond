import AppKit
import DBondCore
import Darwin
import Foundation

enum AgentSessionKind {
    case claude, codex

    init?(service: ServiceView) {
        guard service.destination == "peer" || service.destination == "dot" else { return nil }
        let prefix = service.name.components(separatedBy: " / ").first?.lowercased()
        switch prefix {
        case "claude": self = .claude
        case "codex": self = .codex
        default: return nil
        }
    }
}

enum SessionWakeStatus {
    static func label(_ service: ServiceView) -> String {
        guard !service.paused, service.directSubscribers > 0 else { return "Wake unverified" }
        switch service.wakeState {
        case "verified": return "Wake verified"
        case "pending": return "Testing wake"
        default: return "Wake unverified"
        }
    }

    static func detail(_ service: ServiceView) -> String {
        guard !service.paused, service.directSubscribers > 0 else {
            return service.lastVerifiedAt == nil ? "Wake unverified" : "Wake unverified now · prior test is historical"
        }
        switch service.wakeState {
        case "verified": return "Session acknowledged wake test"
        case "pending": return "Waiting for session wake-test acknowledgment"
        default: return "Wake unverified"
        }
    }
}

/// Read only the owner-local CLI pairing identity. Never return its bearer credential.
enum SessionAlias {
    enum Resolution: Equatable {
        case unique(String), missing, ambiguous([String]), unsafe
    }

    static func resolve(serviceID: String, directory: URL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/D-Bond CLI", isDirectory: true)) -> Resolution {
        guard UUID(uuidString: serviceID) != nil else { return .unsafe }
        let home = FileManager.default.homeDirectoryForCurrentUser.standardizedFileURL
        let folder = directory.standardizedFileURL
        guard folder.path.hasPrefix(home.path + "/") else { return .unsafe }
        var current = home
        for part in folder.path.dropFirst(home.path.count + 1).split(separator: "/") {
            current.appendPathComponent(String(part), isDirectory: true)
            var info = stat()
            if lstat(current.path, &info) != 0 {
                return errno == ENOENT && current.path == folder.path ? .missing : .unsafe
            }
            guard (info.st_mode & S_IFMT) == S_IFDIR, info.st_uid == getuid() else { return .unsafe }
            if current.path == folder.path && info.st_mode & 0o077 != 0 { return .unsafe }
        }
        let names: [String]
        do { names = try FileManager.default.contentsOfDirectory(atPath: folder.path) }
        catch { return .unsafe }
        var matches: [String] = []
        for name in names where name.hasSuffix(".json") {
            let alias = String(name.dropLast(5))
            guard valid(alias) else { continue }
            let path = folder.appendingPathComponent(name).path
            let descriptor = open(path, O_RDONLY | O_NOFOLLOW)
            guard descriptor >= 0 else { continue }
            defer { close(descriptor) }
            var info = stat()
            guard fstat(descriptor, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG,
                  info.st_uid == getuid(), info.st_mode & 0o077 == 0,
                  info.st_size > 0, info.st_size <= 65_536 else { continue }
            guard let data = try? FileHandle(fileDescriptor: descriptor, closeOnDealloc: false).read(upToCount: 65_537),
                  !data.isEmpty, data.count <= 65_536,
                  let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let endpoint = object["endpoint"] as? String,
                  let secret = object["secret"] as? String, !secret.isEmpty,
                  secret.utf8.allSatisfy({ (65...90).contains($0) || (97...122).contains($0) ||
                      (48...57).contains($0) || $0 == 43 || $0 == 47 || $0 == 61 }),
                  let parts = URLComponents(string: endpoint),
                  parts.scheme == "http", parts.host == "127.0.0.1",
                  parts.user == nil, parts.password == nil, parts.query == nil, parts.fragment == nil,
                  let port = parts.port, (1...65_535).contains(port),
                  parts.path == "/v1/services/\(serviceID)/events" else { continue }
            matches.append(alias)
        }
        matches.sort()
        if matches.count == 1 { return .unique(matches[0]) }
        return matches.isEmpty ? .missing : .ambiguous(matches)
    }

    private static func valid(_ alias: String) -> Bool {
        guard (1...64).contains(alias.utf8.count), let first = alias.utf8.first else { return false }
        let letters = Array("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789".utf8)
        let other = Array("._-".utf8)
        return letters.contains(first) && alias.utf8.dropFirst().allSatisfy { letters.contains($0) || other.contains($0) }
    }
}

@MainActor
enum SessionConnectionGuide {
    static func show(_ service: ServiceView, kind: AgentSessionKind) {
        let direct = !service.paused && service.directSubscribers > 0 ? "online" : "offline · waiting for the host session"
        let dot = service.destination == "peer" ? "not used by this direct-only connection" :
            (!service.paused && service.subscribers > 0 ? "online" : "offline")
        let wake = SessionWakeStatus.label(service)
        let wakeDetail = SessionWakeStatus.detail(service)
        let resolution = SessionAlias.resolve(serviceID: service.id)
        let alert = Brand.alert()
        alert.messageText = "Connect session · " + service.name
        var body = "Direct listener: \(direct)\nDot stream: \(dot)\n\(wake): \(wakeDetail)\nConnection ID: \(service.id)\n\n"
        if wake != "Wake verified", let prior = service.lastVerifiedAt {
            body += "Last acknowledged wake test: \(displayTraceTime(prior)) · historical only\n\n"
        }
        body += "A listener is the transport, not proof that an idle chat resumed or answered. Use the host's normal connection flow first; these local commands are for repair or a host Monitor.\n\n"
        var copy: String?
        switch resolution {
        case .unique(let alias):
            let executable = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/bin/dbond").path
            if kind == .claude {
                let command = executable + " session-listen " + alias
                copy = command
                body += "If the host Monitor needs reconnecting, run in that exact Claude session:\n\(command)\n\n"
                body += "Keep the Monitor open. It expires with the host session; restart it in that session to resume saved direct and Dot cursors. An open listener does not itself prove the agent answered.\n\n"
                body += "For a direct_message, answer from that same host with the JSON sender_id and source_id:\n"
                body += executable + " direct-answer " + alias + " SENDER_ID SOURCE_ID \"REPLY\"\n"
                body += "That preserves the original in_reply_to correlation."
            } else {
                body += "In this exact Codex chat, use D-Bond Local Control 0.1.3 `enroll_agent` with its host thread_id, `Codex /` title and alias \(alias). The installed per-chat listener holds this pairing's direct stream and queues a stable event pointer to that same chat across restarts.\n\n"
                body += "The chat fetches each message with `receive_direct_messages` and answers with `answer_direct_message` using the original sender_id and source_id. A queued pointer or listener ACK is not proof that the model read or answered it."
            }
        case .missing:
            if kind == .codex {
                body += "No owner-local CLI alias maps to this exact connection ID. From this exact Codex chat, use Local Control `enroll_agent` with the host thread_id and `Codex /` title to create or reuse its pairing and attach its per-chat listener. Do not guess another chat's ID."
            } else {
                body += "No owner-local CLI alias maps to this exact connection ID. A host adapter may still use its own connection. If its listener remains offline, link or enroll from the intended session before using a CLI repair command."
            }
        case .ambiguous(let aliases):
            body += "Several owner-local aliases map to this exact connection: \(aliases.joined(separator: ", ")). Choose the alias belonging to the intended host session; D-Bond will not guess."
        case .unsafe:
            body += "The local alias store could not be safely inspected. Check its owner-only directory and pairing files in the CLI."
        }
        alert.informativeText = body
        if copy != nil { alert.addButton(withTitle: "Copy listener command") }
        alert.addButton(withTitle: "Done")
        if let copy, alert.runModal() == .alertFirstButtonReturn {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(copy, forType: .string)
        }
    }
}
