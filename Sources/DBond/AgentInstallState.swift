import Darwin
import Foundation

/// Read-only host installation evidence. A plugin can be installed without a
/// D-Bond pairing, an active listener, or an agent turn in the current chat.
enum AgentInstallState {
    enum Status: String { case installed, missing, conflict, unavailable }

    struct Component {
        let status: Status
        let enabled: Bool?
        let version: String?
        let source: String?
        let expectedVersion: String?

        var updateAvailable: Bool {
            guard status == .installed, let version, let expectedVersion,
                  let actual = numericVersion(version), let wanted = numericVersion(expectedVersion) else { return false }
            return actual.lexicographicallyPrecedes(wanted)
        }
    }

    struct Codex {
        let plugin: Component
        let directMCP: Component
    }

    /// Detect official local or GitHub marketplace installations and the
    /// separately registered direct-message stdio server.
    static func codex(expectedVersion: String, expectedLauncher: URL) -> Codex {
        guard let cli = executable("codex") else {
            return inspectCodex(pluginJSON: nil, mcpJSON: nil,
                                expectedVersion: expectedVersion, expectedLauncher: expectedLauncher)
        }
        return inspectCodex(pluginJSON: command(cli, ["plugin", "list", "--json"]),
                            mcpJSON: command(cli, ["mcp", "list", "--json"]),
                            expectedVersion: expectedVersion, expectedLauncher: expectedLauncher)
    }

    static func claudeCode(expectedVersion: String) -> Component {
        guard let cli = executable("claude") else { return empty(.unavailable, expectedVersion) }
        return inspectClaudeCode(pluginJSON: command(cli, ["plugin", "list", "--json"]),
                                 expectedVersion: expectedVersion)
    }

    // The JSON entry points are also the fixture seam; they never invoke a
    // host or open a pairing store.
    static func inspectCodex(pluginJSON: Data?, mcpJSON: Data?, expectedVersion: String,
                             expectedLauncher: URL) -> Codex {
        let plugin: Component
        if let pluginJSON,
           let document = try? JSONSerialization.jsonObject(with: pluginJSON) as? [String: Any],
           let installed = document["installed"] as? [[String: Any]] {
            let official = installed.filter { row in
                ["d-bond-local-control@d-bond-local", "d-bond-local-control@dbond-github"]
                    .contains(row["pluginId"] as? String ?? "") && row["installed"] as? Bool == true
            }
            if let row = official.first {
                let identifier = row["pluginId"] as? String ?? ""
                let marketplace = String(identifier.split(separator: "@").last ?? "")
                plugin = Component(status: .installed, enabled: row["enabled"] as? Bool,
                                   version: row["version"] as? String,
                                   source: "Codex marketplace: \(marketplace)", expectedVersion: expectedVersion)
            } else if installed.contains(where: { ($0["pluginId"] as? String ?? "").hasPrefix("d-bond-local-control@") && $0["installed"] as? Bool == true }) {
                plugin = empty(.conflict, expectedVersion, source: "Different D-Bond marketplace")
            } else { plugin = empty(.missing, expectedVersion) }
        } else { plugin = empty(.unavailable, expectedVersion) }

        let directMCP: Component
        if let mcpJSON,
           let rows = try? JSONSerialization.jsonObject(with: mcpJSON) as? [[String: Any]] {
            let named = rows.first { $0["name"] as? String == "d-bond-agent" }
            let matching = rows.first { matchesLauncher($0, expectedLauncher) }
            if let row = matching {
                directMCP = Component(status: .installed, enabled: row["enabled"] as? Bool,
                                      version: nil, source: "Codex MCP: \(row["name"] as? String ?? "D-Bond")",
                                      expectedVersion: nil)
            } else if named != nil {
                directMCP = empty(.conflict, nil, source: "d-bond-agent uses another command")
            } else { directMCP = empty(.missing, nil) }
        } else { directMCP = empty(.unavailable, nil) }
        return Codex(plugin: plugin, directMCP: directMCP)
    }

    static func inspectClaudeCode(pluginJSON: Data?, expectedVersion: String) -> Component {
        guard let pluginJSON,
              let rows = try? JSONSerialization.jsonObject(with: pluginJSON) as? [[String: Any]] else {
            return empty(.unavailable, expectedVersion)
        }
        let official = ["d-bond@d-bond-local", "d-bond@dbond-github"]
        if let row = rows.first(where: { official.contains($0["id"] as? String ?? "") }) {
            let id = row["id"] as? String ?? ""
            let marketplace = String(id.split(separator: "@").last ?? "")
            return Component(status: .installed, enabled: row["enabled"] as? Bool,
                             version: row["version"] as? String,
                             source: "Claude Code marketplace: \(marketplace) · \(row["scope"] as? String ?? "unknown scope")",
                             expectedVersion: expectedVersion)
        }
        if rows.contains(where: { ($0["id"] as? String ?? "").hasPrefix("d-bond@") }) {
            return empty(.conflict, expectedVersion, source: "Different D-Bond marketplace")
        }
        return empty(.missing, expectedVersion)
    }

    private static func empty(_ status: Status, _ wanted: String?, source: String? = nil) -> Component {
        Component(status: status, enabled: nil, version: nil, source: source, expectedVersion: wanted)
    }

    private static func numericVersion(_ value: String) -> [Int]? {
        let parts = value.split(separator: ".")
        guard !parts.isEmpty, parts.count <= 4 else { return nil }
        var numbers = [Int]()
        for part in parts {
            guard !part.isEmpty, part.allSatisfy({ $0.isASCII && $0.isNumber }),
                  let number = Int(part) else { return nil }
            numbers.append(number)
        }
        while numbers.count < 4 { numbers.append(0) }
        return numbers
    }

    private static func matchesLauncher(_ row: [String: Any], _ launcher: URL) -> Bool {
        guard let transport = row["transport"] as? [String: Any],
              transport["type"] as? String == "stdio",
              transport["command"] as? String == "/bin/sh",
              transport["args"] as? [String] == [launcher.path] else { return false }
        return true
    }

    private static func executable(_ name: String) -> URL? {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let local = "\(home)/.local/bin/\(name)"
        let candidates = name == "codex"
            ? ["/opt/homebrew/bin/\(name)", "/usr/local/bin/\(name)", local, "/usr/bin/\(name)"]
            : [local, "/opt/homebrew/bin/\(name)", "/usr/local/bin/\(name)", "/usr/bin/\(name)"]
        return candidates.first(where: { FileManager.default.isExecutableFile(atPath: $0) }).map(URL.init(fileURLWithPath:))
    }

    private final class Capture {
        private let lock = NSLock()
        private(set) var overflow = false
        private var bytes = Data()
        func append(_ data: Data) {
            lock.lock(); defer { lock.unlock() }
            if bytes.count + data.count > 1_048_576 { overflow = true }
            else if !overflow { bytes.append(data) }
        }
        func result() -> Data? {
            lock.lock(); defer { lock.unlock() }
            return overflow ? nil : bytes
        }
    }

    /// Exact executable, no shell, no input, bounded runtime and output. No
    /// command here performs installation, configuration, login, or pairing.
    private static func command(_ cli: URL, _ args: [String]) -> Data? {
        let process = Process()
        let output = Pipe()
        let capture = Capture()
        let drained = DispatchSemaphore(value: 0)
        let exited = DispatchSemaphore(value: 0)
        process.executableURL = cli
        // launchd's GUI PATH lacks Homebrew's Node, which the Codex executable needs.
        var environment = ProcessInfo.processInfo.environment
        let userBin = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/bin").path
        environment["PATH"] = [cli.deletingLastPathComponent().path,"/opt/homebrew/bin","/usr/local/bin",userBin,"/usr/bin","/bin","/usr/sbin","/sbin"].joined(separator:":")
        process.environment = environment
        process.arguments = args
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        process.terminationHandler = { _ in exited.signal() }
        DispatchQueue.global(qos: .utility).async {
            while true {
                let chunk = output.fileHandleForReading.readData(ofLength: 65_536)
                if chunk.isEmpty { break }
                capture.append(chunk)
            }
            drained.signal()
        }
        do { try process.run() } catch { output.fileHandleForWriting.closeFile(); return nil }
        output.fileHandleForWriting.closeFile()
        if exited.wait(timeout: .now() + 6) == .timedOut {
            if process.isRunning { process.terminate() }
            if exited.wait(timeout: .now() + 1) == .timedOut {
                kill(process.processIdentifier, SIGKILL)
                _ = exited.wait(timeout: .now() + 1)
            }
            return nil
        }
        guard drained.wait(timeout: .now() + 1) == .success,
              process.terminationStatus == 0 else { return nil }
        return capture.result()
    }
}
