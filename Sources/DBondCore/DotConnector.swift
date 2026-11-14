import Foundation

/// Public, credential-free output from the D-Bond-owned connector CLI.
public struct DotConnectorStatus: Decodable, Equatable {
    public var configured: Bool
    public var running: Bool
    public var subscribed: Bool
    public var publicURL: String?
    public var redirectURI: String?
    public var mcpURL: String?
    public var state: String?
    public var reason: String?
    public var pid: Int?
    public var identity: Identity?
    public struct Identity: Decodable, Equatable {
        public var connected: Bool
        public var activeSubscriptions: Int
        enum CodingKeys: String, CodingKey { case connected; case activeSubscriptions = "active_subscriptions" }
    }
    enum CodingKeys: String, CodingKey {
        case configured, running, subscribed, state, reason, identity, pid
        case publicURL = "public_url", redirectURI = "redirect_uri", mcpURL = "mcp_url"
    }
    public var canSelectOwnedRoute: Bool {
        configured && running && subscribed && identity?.connected == true && identity?.activeSubscriptions == 1
    }
}

/// Returned only by an explicit, owner-initiated setup disclosure.
public struct DotConnectorSetup: Decodable {
    public let mcpURL: String
    public let clientID: String
    public let ownerKey: String
    enum CodingKeys: String, CodingKey { case mcpURL = "mcp_url", clientID = "client_id", ownerKey = "owner_key" }
}

public enum DotConnectorError: LocalizedError {
    case unavailable, invalidURL, commandFailed, timeout, unverifiedIdentity, stopExternalHost, pluginExportFailed
    public var errorDescription: String? {
        switch self {
        case .unavailable: return "The bundled Dot connector is unavailable. Reinstall a complete D-Bond build."
        case .invalidURL: return "Enter a public HTTPS origin and the HTTPS OAuth redirect URL from your app setup."
        case .commandFailed: return "The Dot connector could not complete this action. Check its setup and try again."
        case .timeout: return "The Dot connector did not finish in time. Inspect its status before retrying."
        case .unverifiedIdentity: return "Connect your Dot and verify exactly one active d-bond subscription before selecting this route."
        case .pluginExportFailed: return "The private plugin could not be exported. Choose one connected-app binding JSON and a new ZIP filename. Keep the exported app identity private."
        case .stopExternalHost: return "Stop the running connector before changing its configuration."
        }
    }
}

/// Serializes host lifecycle changes; never reads or edits the legacy DotBridge.
public final class DotConnector: @unchecked Sendable {
    public static let enabledPreference = "ownedDotConnectorEnabled"
    public static var supportDirectory: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/D-Bond", isDirectory: true)
    }
    public static var routeFile: URL { supportDirectory.appendingPathComponent("dot-route.json") }
    public static var defaultExecutable: URL? {
        #if DBOND_APP_BUNDLE
        return Bundle.main.resourceURL?.appendingPathComponent("dot-host/connector")
        #else
        if Bundle.main.bundleURL.pathExtension == "app" {
            return Bundle.main.resourceURL?.appendingPathComponent("dot-host/connector")
        }
        return URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("adapters/dot-host/connector")
        #endif
    }
    private let executable: URL?
    private let stateDirectory: URL
    private let routeURL: URL
    private let defaults: UserDefaults
    private let queue = DispatchQueue(label: "design.ivg.dbond.dot-connector")
    private let processLock = NSLock()
    private var ownedChild: Process?
    private var recoveryAttempts = 0
    private var watchedHost: DispatchSourceProcess?
    private var observedHostPID: Int?
    private var watchedPID: Int? {
        get { processLock.lock(); defer { processLock.unlock() }; return observedHostPID }
        set { processLock.lock(); defer { processLock.unlock() }; observedHostPID = newValue }
    }
    private var ending = false
    private var shuttingDown: Bool {
        get { processLock.lock(); defer { processLock.unlock() }; return ending }
        set { processLock.lock(); defer { processLock.unlock() }; ending = newValue }
    }
    public var onHostRecovered: (() -> Void)?
    private var child: Process? {
        get { processLock.lock(); defer { processLock.unlock() }; return ownedChild }
        set { processLock.lock(); defer { processLock.unlock() }; ownedChild = newValue }
    }

    public init(executable: URL? = DotConnector.defaultExecutable,
                stateDirectory: URL = DotConnector.supportDirectory.appendingPathComponent("Dot Connector", isDirectory: true),
                routeURL: URL = DotConnector.routeFile, defaults: UserDefaults = .standard) {
        self.executable = executable
        self.stateDirectory = stateDirectory
        self.routeURL = routeURL
        self.defaults = defaults
    }

    public static func validSetup(publicURL: String, redirectURI: String) -> Bool {
        guard let origin = URLComponents(string: publicURL), origin.scheme == "https",
              origin.host?.isEmpty == false, origin.user == nil, origin.password == nil,
              origin.query == nil, origin.fragment == nil, ["", "/"].contains(origin.path),
              let redirect = URLComponents(string: redirectURI), redirect.scheme == "https",
              redirect.host?.isEmpty == false, redirect.user == nil, redirect.password == nil,
              redirect.fragment == nil else { return false }
        return true
    }

    public var selectedRoute: String {
        guard let data = try? Data(contentsOf: routeURL),
              let value = try? JSONSerialization.jsonObject(with: data) as? [String: String],
              value["route"] == "owned" else { return "legacy" }
        return "owned"
    }

    private func execute(_ arguments: [String], timeout: TimeInterval = 10) throws -> Data {
        guard let executable, FileManager.default.isExecutableFile(atPath: executable.path) else { throw DotConnectorError.unavailable }
        let process = Process(), output = Pipe()
        process.executableURL = executable
        process.arguments = ["--state-dir", stateDirectory.path] + arguments
        process.standardOutput = output
        // CLI errors must be deliberately safe to display; never surface raw output.
        process.standardError = FileHandle.nullDevice
        try process.run()
        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
        guard !process.isRunning else {
            process.terminate()
            throw DotConnectorError.timeout
        }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        guard process.terminationStatus == 0, data.count <= 65_536 else { throw DotConnectorError.commandFailed }
        return data
    }

    private func command(_ arguments: [String], timeout: TimeInterval = 10) throws -> DotConnectorStatus {
        let data = try execute(arguments, timeout: timeout)
        guard let status = try? JSONDecoder().decode(DotConnectorStatus.self, from: data) else { throw DotConnectorError.commandFailed }
        return status
    }

    private func startHost() throws -> DotConnectorStatus {
        var status = try command(["status"])
        guard status.configured else { return status }
        if status.running {
            if child == nil { watchExistingHost(status) }
            return status
        }
        if shuttingDown { return status }
        if let child, child.isRunning { throw DotConnectorError.timeout }
        guard let executable else { throw DotConnectorError.unavailable }
        let process = Process()
        process.executableURL = executable
        process.arguments = ["--state-dir", stateDirectory.path, "serve"]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        process.terminationHandler = { [weak self] exited in
            guard let self else { return }
            self.queue.async { self.recover(after: exited) }
        }
        child = process
        do { try process.run() } catch { child = nil; throw error }
        let deadline = Date().addingTimeInterval(8)
        repeat {
            status = try command(["status"])
            if status.running { return status }
            if !process.isRunning { throw DotConnectorError.commandFailed }
            Thread.sleep(forTimeInterval: 0.2)
        } while Date() < deadline
        // Leave a possibly starting child available for inspection; never spawn
        // another just because startup readiness took longer than expected.
        throw DotConnectorError.timeout
    }

    private func watchExistingHost(_ status: DotConnectorStatus) {
        guard let pid = status.pid, pid > 0, pid <= Int(Int32.max), watchedPID != pid else { return }
        watchedHost?.cancel()
        let watcher = DispatchSource.makeProcessSource(identifier: pid_t(pid), eventMask: .exit, queue: queue)
        watchedPID = pid
        watchedHost = watcher
        watcher.setEventHandler { [weak self] in
            guard let self, self.watchedPID == pid else { return }
            self.watchedHost?.cancel()
            self.watchedHost = nil
            self.watchedPID = nil
            self.scheduleRecovery()
        }
        watcher.resume()
    }

    private func recover(after exited: Process) {
        guard child === exited else { return }
        child = nil
        scheduleRecovery()
    }

    private func scheduleRecovery() {
        guard !shuttingDown, defaults.bool(forKey: Self.enabledPreference), recoveryAttempts < 5 else { return }
        recoveryAttempts += 1
        let delay = min(30.0, pow(2.0, Double(recoveryAttempts)))
        queue.asyncAfter(deadline: .now() + delay) {
            guard !self.shuttingDown, self.child == nil, self.defaults.bool(forKey: Self.enabledPreference) else { return }
            do {
                let status = try self.startHost()
                if status.running { DispatchQueue.main.async { self.onHostRecovered?() } }
            } catch {
                // A child that exited has its own termination callback; missing
                // configuration/runtime requires an explicit new setup attempt.
            }
        }
    }

    private func perform<T>(_ action: @escaping () throws -> T,
                            completion: @escaping (Result<T, Error>) -> Void) {
        queue.async {
            let result = Result { try action() }
            DispatchQueue.main.async { completion(result) }
        }
    }

    public func setup(completion: @escaping (Result<DotConnectorSetup, Error>) -> Void) {
        perform({
            let data = try self.execute(["setup"])
            guard let setup = try? JSONDecoder().decode(DotConnectorSetup.self, from: data) else { throw DotConnectorError.commandFailed }
            return setup
        }, completion: completion)
    }

    public func exportPlugin(binding: URL, output: URL, completion: @escaping (Result<Void, Error>) -> Void) {
        perform({
            guard let resources = Bundle.main.resourceURL,
                  Bundle.main.bundleURL.pathExtension == "app" else { throw DotConnectorError.unavailable }
            let python = resources.appendingPathComponent("dot-runtime/python/bin/python3.13")
            let helper = resources.appendingPathComponent("dot-plugin/build_private.py")
            guard FileManager.default.isExecutableFile(atPath: python.path),
                  FileManager.default.fileExists(atPath: helper.path) else { throw DotConnectorError.unavailable }
            let process = Process()
            process.executableURL = python
            process.arguments = ["-I", "-B", helper.path, "--binding", binding.path, "--output", output.path]
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice
            try process.run()
            process.waitUntilExit()
            guard process.terminationStatus == 0 else { throw DotConnectorError.pluginExportFailed }
        }, completion: completion)
    }

    public func inspect(completion: @escaping (Result<DotConnectorStatus, Error>) -> Void) {
        perform({ try self.command(["status"]) }, completion: completion)
    }

    public func configure(publicURL: String, redirectURI: String,
                          completion: @escaping (Result<DotConnectorStatus, Error>) -> Void) {
        perform({
            guard Self.validSetup(publicURL: publicURL, redirectURI: redirectURI) else { throw DotConnectorError.invalidURL }
            let before = try self.command(["status"])
            if before.running {
                guard let child = self.child else { throw DotConnectorError.stopExternalHost }
                self.child = nil
                if child.isRunning { child.terminate(); child.waitUntilExit() }
            }
            _ = try self.command(["configure", "--public-url", publicURL, "--redirect-uri", redirectURI], timeout: 300)
            // Configuration explicitly starts this owned host for subscription
            // setup, but does not migrate or select the existing legacy route.
            self.defaults.set(true, forKey: Self.enabledPreference)
            self.recoveryAttempts = 0
            return try self.startHost()
        }, completion: completion)
    }

    public func start(completion: @escaping (Result<DotConnectorStatus, Error>) -> Void) {
        perform({
            self.recoveryAttempts = 0
            let status = try self.startHost()
            if status.configured { self.defaults.set(true, forKey: Self.enabledPreference) }
            return status
        }, completion: completion)
    }

    public func startIfEnabled(completion: @escaping (Result<DotConnectorStatus, Error>) -> Void) {
        guard defaults.bool(forKey: Self.enabledPreference) else { return }
        perform({ try self.startHost() }, completion: completion)
    }

    public func stop(completion: @escaping (Result<DotConnectorStatus, Error>) -> Void) {
        perform({
            self.defaults.set(false, forKey: Self.enabledPreference)
            self.watchedHost?.cancel()
            self.watchedHost = nil
            self.watchedPID = nil
            if let child = self.child {
                if child.isRunning { child.terminate(); child.waitUntilExit() }
                self.child = nil
            } else if try self.command(["status"]).running {
                _ = try self.command(["stop"])
                for _ in 0..<20 {
                    let status = try self.command(["status"])
                    if !status.running { return status }
                    Thread.sleep(forTimeInterval: 0.1)
                }
            }
            return try self.command(["status"])
        }, completion: completion)
    }

    private static var unavailableStatus: DotConnectorStatus {
        // Safe fixed fallback permits reverting routing even after a missing or
        // broken owned runtime; it contains no connection or identity claims.
        DotConnectorStatus(configured: false, running: false, subscribed: false)
    }

    public func selectRoute(_ route: String, completion: @escaping (Result<DotConnectorStatus, Error>) -> Void) {
        perform({
            let status: DotConnectorStatus
            if route == "legacy" {
                status = (try? self.command(["status"])) ?? Self.unavailableStatus
            } else {
                status = try self.command(["status"])
                guard route == "owned" && status.canSelectOwnedRoute else { throw DotConnectorError.unverifiedIdentity }
            }
            try FileManager.default.createDirectory(at: self.routeURL.deletingLastPathComponent(), withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            let data = try JSONSerialization.data(withJSONObject: ["route": route], options: [.sortedKeys])
            try data.write(to: self.routeURL, options: [.atomic])
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: self.routeURL.path)
            return status
        }, completion: completion)
    }

    /// Explicit Quit stops its child or an authenticated adopted owned host.
    /// It never signals an adopted PID or contacts the separate legacy runtime.
    public func shutdown() {
        shuttingDown = true
        let adopted = watchedPID != nil
        queue.async {
            self.watchedHost?.cancel()
            self.watchedHost = nil
            self.watchedPID = nil
        }
        if let child, child.isRunning {
            child.terminationHandler = nil
            child.terminate()
        } else if adopted {
            // The CLI authenticates the store identity before shutdown. Keep
            // normal app termination bounded even if that host has disappeared.
            _ = try? command(["stop"], timeout: 3)
        }
    }
}
