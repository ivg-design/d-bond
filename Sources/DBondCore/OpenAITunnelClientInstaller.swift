import Foundation

/// Installs the provider's supported macOS client, independently of starting a tunnel.
public enum OpenAITunnelClientInstaller {
    public static let homebrewSetupURL = URL(string: "https://brew.sh/")!
    public static let instructionsURL = URL(string: "https://github.com/openai/tunnel-client#install-with-homebrew")!
    public static let formula = "openai/tools/tunnel-client"

    private static var brewBins: [URL] {
        #if arch(arm64)
        let paths = ["/opt/homebrew/bin", "/usr/local/bin"]
        #else
        let paths = ["/usr/local/bin", "/opt/homebrew/bin"]
        #endif
        return paths.map { URL(fileURLWithPath: $0, isDirectory: true) }
    }

    static func executable(_ name: String, in directories: [URL]) -> URL? {
        directories.map { $0.appendingPathComponent(name) }.first { url in
            (try? url.resolvingSymlinksInPath().resourceValues(forKeys: [.isRegularFileKey]).isRegularFile) == true
                && FileManager.default.isExecutableFile(atPath: url.path)
        }
    }

    public static var clientURL: URL? {
        let userBin = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/bin", isDirectory: true)
        return client(in: [userBin] + brewBins)
    }

    static func client(in directories: [URL]) -> URL? {
        guard let selected = executable("tunnel-client", in: directories) else { return nil }
        let wrapper = selected.resolvingSymlinksInPath()
        let bin = wrapper.deletingLastPathComponent()
        let keg = bin.deletingLastPathComponent()
        let rack = keg.deletingLastPathComponent()
        guard bin.lastPathComponent == "bin", rack.lastPathComponent == "tunnel-client",
              rack.deletingLastPathComponent().lastPathComponent == "Cellar" else { return selected }

        // Homebrew's write_exec_script wraps the native program. A running
        // client must stay identifiable after Homebrew relinks its bin directory.
        let native = keg.appendingPathComponent("libexec/tunnel-client")
        guard native.resolvingSymlinksInPath().path == native.path,
              executable("tunnel-client", in: [native.deletingLastPathComponent()]) != nil,
              let handle = try? FileHandle(forReadingFrom: wrapper) else { return selected }
        defer { try? handle.close() }
        let prefix = "#!/bin/bash\nexec \""
        let suffix = "\" \"$@\"\n"
        guard let data = try? handle.read(upToCount: 4097), data.count <= 4096,
              let script = String(data: data, encoding: .utf8),
              script.hasPrefix(prefix), script.hasSuffix(suffix) else { return selected }
        let target = String(script.dropFirst(prefix.count).dropLast(suffix.count))
        guard target.hasPrefix("/"),
              URL(fileURLWithPath: target).resolvingSymlinksInPath().path == native.path else { return selected }
        return native
    }

    public static var homebrewURL: URL? { executable("brew", in: brewBins) }

    public enum Failure: LocalizedError {
        case missingHomebrew, tap, install, timedOut, verification
        public var errorDescription: String? {
            switch self {
            case .missingHomebrew: return "Install Homebrew, then return here and click Install OpenAI tunnel client."
            case .tap: return "Homebrew could not open OpenAI’s official tools tap. Check your network or the configured openai/tools tap, then retry."
            case .install: return "Homebrew could not install OpenAI’s tunnel client. Run brew install openai/tools/tunnel-client in Terminal for the Homebrew diagnostic."
            case .timedOut: return "Homebrew did not finish in time. Check the Homebrew installation in Terminal, then retry."
            case .verification: return "Homebrew finished, but its tunnel-client command did not report a version. Check the installation in Terminal."
            }
        }
    }

    private static let installer = InstallCoordinator()

    public static func install() async throws -> URL { try await installer.perform() }

    private actor InstallCoordinator {
        private var current: Task<URL, Error>?

        func perform() async throws -> URL {
            if let current { return try await current.value }
            let task = Task.detached(priority: .userInitiated) {
                try installIfNeeded(findClient: { clientURL }, brew: homebrewURL, run: runProgram)
            }
            current = task
            defer { current = nil }
            return try await task.value
        }
    }

    static func installIfNeeded(findClient: () -> URL?, brew: URL?, run: (URL, [String], TimeInterval) throws -> Bool) throws -> URL {
        if let client = findClient() { return client }
        guard let brew else { throw Failure.missingHomebrew }
        // Supplying the canonical remote makes Homebrew reject a previously
        // configured tap with a conflicting URL. It never rewrites that tap.
        guard try run(brew, ["tap", "openai/tools", "https://github.com/openai/homebrew-tools"], 180) else { throw Failure.tap }
        if let client = findClient() { return client }
        guard try run(brew, ["install", "--formula", formula], 600) else { throw Failure.install }
        guard let client = findClient(), try run(client, ["--version"], 10) else { throw Failure.verification }
        return client
    }

    private static func runProgram(_ executable: URL, arguments: [String], timeout: TimeInterval) throws -> Bool {
        let process = Process(), finished = DispatchSemaphore(value: 0)
        process.executableURL = executable
        process.arguments = arguments
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = ([executable.deletingLastPathComponent().path] + brewBins.map(\.path) + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]).joined(separator: ":")
        environment["NONINTERACTIVE"] = "1"
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["HOMEBREW_NO_AUTO_UPDATE"] = "1"
        environment["HOMEBREW_NO_INSTALL_CLEANUP"] = "1"
        environment["HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK"] = "1"
        process.environment = environment
        process.terminationHandler = { _ in finished.signal() }
        try process.run()
        guard finished.wait(timeout: .now() + timeout) == .success else {
            if process.isRunning { process.terminate() }
            throw Failure.timedOut
        }
        return process.terminationStatus == 0
    }
}
