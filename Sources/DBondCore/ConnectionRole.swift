import Foundation

/// Describes the paired party. A route to Dot may belong to any of these roles.
public enum ConnectionRole: String {
    case codexSession, claudeSession, dotSession, workflow

    static func legacy(name: String, destination: String?, chromeOrigin: String?) -> Self {
        guard destination == "dot" || destination == "peer", chromeOrigin == nil else { return .workflow }
        let parts = name.components(separatedBy: " / ")
        guard parts.count == 1 || !parts[1].isEmpty else { return .workflow }
        switch parts[0].lowercased() {
        case "codex": return .codexSession
        case "claude": return .claudeSession
        case "dot": return .dotSession
        default: return .workflow
        }
    }
}

extension Service {
    var connectionRole: ConnectionRole {
        if let role { return ConnectionRole(rawValue: role) ?? .workflow }
        return ConnectionRole.legacy(name: name, destination: destination, chromeOrigin: chromeOrigin)
    }
}
