import AppKit
import ServiceManagement

/// Controls the installed launchd job, falling back to the original app login
/// registration for builds that have not been installed with the supervisor.
@MainActor
enum LoginRegistration {
    private static var label: String { "\(Bundle.main.bundleIdentifier ?? "design.ivg.dbond").supervisor" }
    private static var agentURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/\(label).plist")
    }
    private static var domain: String { "gui/\(getuid())" }
    static var usesSupervisor: Bool { FileManager.default.fileExists(atPath: agentURL.path) }

    static var isEnabled: Bool {
        if usesSupervisor {
            let result = launchctl("print-disabled", domain)
            guard result.status == 0 else { return false }
            return !result.output.contains("\"\(label)\" => disabled")
        }
        return SMAppService.mainApp.status == .enabled
    }

    static func setEnabled(_ enabled: Bool) throws {
        if usesSupervisor {
            let result = launchctl(enabled ? "enable" : "disable", "\(domain)/\(label)")
            guard result.status == 0 else {
                throw NSError(domain: "D-Bond Login", code: Int(result.status), userInfo: [
                    NSLocalizedDescriptionKey: result.output.isEmpty ? "launchctl could not change D-Bond's login setting." : result.output
                ])
            }
            return
        }
        if enabled {
            if SMAppService.mainApp.status != .enabled {
                try SMAppService.mainApp.register()
            }
        } else if SMAppService.mainApp.status == .enabled {
            try SMAppService.mainApp.unregister()
        }
    }

    private static func launchctl(_ arguments: String...) -> (status: Int32, output: String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = arguments
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        do {
            try process.run()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            return (process.terminationStatus, String(decoding: data, as: UTF8.self))
        } catch {
            return (-1, error.localizedDescription)
        }
    }
}
