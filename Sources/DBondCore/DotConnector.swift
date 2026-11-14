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
    public var tunnelConfigured: Bool?
    public var tunnelID: String?
    public var identity: Identity?
    public struct Identity: Decodable, Equatable {
        public var connected: Bool
        public var activeSubscriptions: Int
        public var tunnelID: String?
        public var tunnelListening: Bool?
        public var tunnelSubscribed: Bool?
        enum CodingKeys: String, CodingKey {
            case connected
            case tunnelID = "tunnel_id", activeSubscriptions = "active_subscriptions", tunnelListening = "tunnel_listening", tunnelSubscribed = "tunnel_subscribed"
        }
    }
    enum CodingKeys: String, CodingKey {
        case configured, running, subscribed, state, reason, identity, pid
        case publicURL = "public_url", redirectURI = "redirect_uri", mcpURL = "mcp_url", tunnelConfigured = "tunnel_configured", tunnelID = "tunnel_id"
    }
    public func servesTunnel(_ id: String) -> Bool {
        !id.isEmpty && id == tunnelID && id == identity?.tunnelID && tunnelConfigured == true && identity?.tunnelListening == true
    }

    public func canSelectTunnelRoute(_ id: String) -> Bool {
        canSelectOwnedRoute && servesTunnel(id) && identity?.tunnelSubscribed == true
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
    case unavailable, invalidURL, invalidConfiguration, commandFailed, timeout, unverifiedIdentity, stopExternalHost, pluginExportFailed
    public var errorDescription: String? {
        switch self {
        case .unavailable: return "The bundled Dot connector is unavailable. Reinstall a complete D-Bond build."
        case .invalidURL: return "Enter a public HTTPS origin and the HTTPS OAuth redirect URL from your app setup."
        case .invalidConfiguration: return "The saved Dot connector configuration needs repair. Open Dot setup to correct it."
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
    private let initialRecoveryDelay: TimeInterval
    private var nextRecoveryDelay: TimeInterval
    private var recovery: DispatchWorkItem?
    private var readySince: Date?
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
                routeURL: URL = DotConnector.routeFile, defaults: UserDefaults = .standard,
                recoveryDelay: TimeInterval = 2) {
        self.executable = executable
        self.stateDirectory = stateDirectory
        self.routeURL = routeURL
        self.defaults = defaults
        initialRecoveryDelay = max(0.01, min(30, recoveryDelay))
        nextRecoveryDelay = initialRecoveryDelay
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
        guard data.count <= 65_536 else { throw DotConnectorError.commandFailed }
        if process.terminationStatus != 0 {
            if arguments == ["status"], let status = try? JSONDecoder().decode(DotConnectorStatus.self, from: data),
               status.state == "blocked", let reason = status.reason,
               ["ValueError", "ValidationError", "TOMLDecodeError", "PermissionError"].contains(reason) {
                throw DotConnectorError.invalidConfiguration
            }
            throw DotConnectorError.commandFailed
        }
        return data
    }

    private func command(_ arguments: [String], timeout: TimeInterval = 10) throws -> DotConnectorStatus {
        let data = try execute(arguments, timeout: timeout)
        guard let status = try? JSONDecoder().decode(DotConnectorStatus.self, from: data) else { throw DotConnectorError.commandFailed }
        return status
    }

    private func startHost(enableIfConfigured: Bool) throws -> DotConnectorStatus {
        var status = try command(["status"])
        guard status.configured else { return status }
        if enableIfConfigured { defaults.set(true, forKey: Self.enabledPreference) }
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

    private func resetRecovery() {
        recovery?.cancel()
        recovery = nil
        nextRecoveryDelay = initialRecoveryDelay
        readySince = nil
    }

    private func startWithRecovery(enableIfConfigured: Bool = false) throws -> DotConnectorStatus {
        do {
            let status = try startHost(enableIfConfigured: enableIfConfigured)
            if status.running { readySince = Date() }
            return status
        } catch DotConnectorError.unavailable {
            throw DotConnectorError.unavailable
        } catch DotConnectorError.invalidConfiguration {
            throw DotConnectorError.invalidConfiguration
        } catch {
            scheduleRecovery()
            throw error
        }
    }

    private func scheduleRecovery() {
        guard !shuttingDown, defaults.bool(forKey: Self.enabledPreference), recovery == nil else { return }
        if let readySince, Date().timeIntervalSince(readySince) >= 30 { nextRecoveryDelay = initialRecoveryDelay }
        readySince = nil
        let delay = nextRecoveryDelay
        nextRecoveryDelay = min(30, delay * 2)
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.recovery = nil
            guard !self.shuttingDown, self.defaults.bool(forKey: Self.enabledPreference) else { return }
            // Recheck identity even if the previous launch took longer to become
            // ready. startHost reuses that process and never spawns a second one.
            if let status = try? self.startWithRecovery(), status.running {
                DispatchQueue.main.async { self.onHostRecovered?() }
            }
        }
        recovery = work
        queue.asyncAfter(deadline: .now() + delay, execute: work)
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
            self.resetRecovery()
            return try self.startWithRecovery()
        }, completion: completion)
    }

    /// Adds a private listener to the existing store and keeps the selected workflow route.
    public func configureTunnel(tunnelID: String, completion: @escaping (Result<DotConnectorStatus, Error>) -> Void) {
        perform({
            guard tunnelID.range(of: "^tunnel_[A-Za-z0-9_-]{6,160}$", options: .regularExpression) != nil else { throw DotConnectorError.commandFailed }
            let prepared = try self.command(["configure-tunnel", "--tunnel-id", tunnelID], timeout: 300)
            guard prepared.tunnelConfigured == true, prepared.tunnelID == tunnelID else { throw DotConnectorError.commandFailed }
            self.defaults.set(true, forKey: Self.enabledPreference)
            if prepared.running && !prepared.servesTunnel(tunnelID) {
                try self.stopHost()
            }
            self.resetRecovery()
            let running = try self.startWithRecovery()
            guard running.servesTunnel(tunnelID) else { throw DotConnectorError.commandFailed }
            return running
        }, completion: completion)
    }

    public func start(completion: @escaping (Result<DotConnectorStatus, Error>) -> Void) {
        perform({
            self.resetRecovery()
            return try self.startWithRecovery(enableIfConfigured: true)
        }, completion: completion)
    }

    public func startIfEnabled(completion: @escaping (Result<DotConnectorStatus, Error>) -> Void) {
        guard defaults.bool(forKey: Self.enabledPreference) else { return }
        perform({ try self.startWithRecovery() }, completion: completion)
    }

    public func stop(completion: @escaping (Result<DotConnectorStatus, Error>) -> Void) {
        perform({
            self.defaults.set(false, forKey: Self.enabledPreference)
            self.resetRecovery()
            try self.stopHost()
            return try self.command(["status"])
        }, completion: completion)
    }

    private func stopHost() throws {
        watchedHost?.cancel()
        watchedHost = nil
        watchedPID = nil
        if let child {
            self.child = nil
            if child.isRunning { child.terminate(); child.waitUntilExit() }
        } else if try command(["status"]).running {
            _ = try command(["stop"])
            for _ in 0..<20 {
                if try !command(["status"]).running { return }
                Thread.sleep(forTimeInterval: 0.1)
            }
            throw DotConnectorError.timeout
        }
    }

    private static var unavailableStatus: DotConnectorStatus {
        // Safe fixed fallback permits reverting routing even after a missing or
        // broken owned runtime; it contains no connection or identity claims.
        DotConnectorStatus(configured: false, running: false, subscribed: false)
    }

    public func selectRoute(_ route: String, requireTunnelID: String? = nil, completion: @escaping (Result<DotConnectorStatus, Error>) -> Void) {
        perform({
            let status: DotConnectorStatus
            if route == "legacy" {
                status = (try? self.command(["status"])) ?? Self.unavailableStatus
            } else {
                status = try self.command(["status"])
                guard route == "owned", requireTunnelID.map(status.canSelectTunnelRoute) ?? status.canSelectOwnedRoute else { throw DotConnectorError.unverifiedIdentity }
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
