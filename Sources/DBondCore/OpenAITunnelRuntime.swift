import Foundation
import Security
import LocalAuthentication
import Darwin

@MainActor
public protocol OpenAITunnelCredentialStore: AnyObject {
    func read() throws -> String?
    func write(_ value: String) throws
}

@MainActor
private final class TunnelKeychain: OpenAITunnelCredentialStore {
    private var query: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: "design.ivg.dbond.openai-tunnel",
         kSecAttrAccount as String: "runtime-api-key"]
    }
    func read() throws -> String? {
        var lookup = query
        lookup[kSecReturnData as String] = true
        lookup[kSecMatchLimit as String] = kSecMatchLimitOne
        // A locked Keychain produces one actionable status, never an automatic
        // authorization dialog on each reconnect or readiness refresh.
        let context = LAContext()
        context.interactionNotAllowed = true
        lookup[kSecUseAuthenticationContext as String] = context
        var result: CFTypeRef?
        let status = SecItemCopyMatching(lookup as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data,
              let value = String(data: data, encoding: .utf8) else { throw OpenAITunnelError.keychain }
        return value
    }
    func write(_ value: String) throws {
        let values: [String: Any] = [kSecValueData as String: Data(value.utf8),
            kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly]
        var status = SecItemUpdate(query as CFDictionary, values as CFDictionary)
        if status == errSecItemNotFound {
            status = SecItemAdd(query.merging(values) { _, new in new } as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw OpenAITunnelError.keychain }
    }
}

public enum OpenAITunnelError: LocalizedError {
    case invalidID, missingKey, keychain, alreadyRunning, missingClient, missingHostKey, privateStorage, launchFailed
    public var errorDescription: String? {
        switch self {
        case .invalidID: return "Enter the complete tunnel ID from OpenAI Platform tunnel settings."
        case .missingKey: return "Enter the runtime API key for this tunnel. It will be saved in your login Keychain."
        case .keychain: return "Unlock your login Keychain, or save the tunnel runtime key again."
        case .alreadyRunning: return "A D-Bond tunnel is already running or stopping. Wait for it to stop before changing this setup."
        case .missingClient: return "Install the official tunnel-client with brew install openai/tools/tunnel-client, then try again."
        case .missingHostKey: return "Configure and start D-Bond's local tunnel connector before starting the tunnel client."
        case .privateStorage: return "D-Bond could not safely open its private tunnel runtime files."
        case .launchFailed: return "The tunnel client could not start. Check its installation and saved setup."
        }
    }
}

/// Supervises only D-Bond's explicit tunnel process. No profile discovery, cloud
/// resource changes, public ingress, route selection, or unrelated PID takeover.
@MainActor
public final class OpenAITunnelRuntime {
    public static let changed = Notification.Name("D-Bond.OpenAITunnelRuntimeChanged")
    public static let installURL = URL(string: "https://github.com/openai/tunnel-client/releases/latest")!
    public static var defaultStateDirectory: URL {
        DotConnector.supportDirectory.appendingPathComponent("OpenAI Tunnel", isDirectory: true)
    }
    public private(set) var state = "OpenAI tunnel is stopped."
    public private(set) var running = false
    public private(set) var ready = false
    public var tunnelID: String { defaults.string(forKey: "openAITunnelID") ?? "" }
    public var enabled: Bool { defaults.bool(forKey: "openAITunnelEnabled") }
    public var configured: Bool { Self.validTunnelID(tunnelID) && defaults.bool(forKey: "openAITunnelKeySaved") }
    public var executablePath: String? { owned?.processExecutable ?? executable?.path }
    public var processIdentifier: Int32? { owned?.pid }

    private let defaults: UserDefaults
    private let directory: URL
    private let connectorDirectory: URL
    private let executableOverride: URL?
    private var executable: URL? { executableOverride ?? OpenAITunnelClientInstaller.clientURL }
    private let credentials: OpenAITunnelCredentialStore
    private let retryDelays: [TimeInterval]
    private let pollInterval: TimeInterval
    private let session: URLSession
    private var controllerFD: Int32 = -1
    private var clientFD: Int32 = -1
    private var child: Process?
    private var watcher: DispatchSourceProcess?
    private var owned: OwnedProcess?
    private var timer: Timer?
    private var retry: DispatchWorkItem?
    private var stopping = false
    private var generation = 0
    private var failures = 0
    private var startedAt = Date()
    private var probePending = false
    private var credentialRejected = false

    private struct OwnedProcess: Codable {
        let pid: Int32
        let startSeconds: UInt64
        let startMicroseconds: UInt64
        let launchExecutable: String
        let processExecutable: String
        let tunnelID: String
        let runDirectory: String
        let arguments: [String]
    }

    public init(defaults: UserDefaults = .standard,
                stateDirectory: URL? = nil,
                connectorStateDirectory: URL = DotConnector.supportDirectory.appendingPathComponent("Dot Connector", isDirectory: true),
                executable: URL? = nil,
                credentialStore: OpenAITunnelCredentialStore? = nil,
                retryDelays: [TimeInterval] = [1, 2, 4, 8, 15, 30, 60], pollInterval: TimeInterval = 2,
                session: URLSession? = nil) {
        self.defaults = defaults
        self.directory = (stateDirectory ?? Self.defaultStateDirectory).standardizedFileURL.resolvingSymlinksInPath()
        self.connectorDirectory = connectorStateDirectory.standardizedFileURL.resolvingSymlinksInPath()
        self.executableOverride = executable
        self.credentials = credentialStore ?? TunnelKeychain()
        self.retryDelays = retryDelays
        self.pollInterval = pollInterval
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 3
        configuration.urlCache = nil
        self.session = session ?? URLSession(configuration: configuration)
    }

    public static func validTunnelID(_ value: String) -> Bool {
        value.range(of: "^tunnel_(?:[a-z0-9]{4}_)?[a-z0-9]{32}$", options: .regularExpression) != nil
    }

    public func configure(tunnelID raw: String, apiKey: String?) throws {
        guard !running, owned == nil else { throw OpenAITunnelError.alreadyRunning }
        try assertNoSurvivingClient()
        let id = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard Self.validTunnelID(id) else { throw OpenAITunnelError.invalidID }
        let replacement = apiKey?.trimmingCharacters(in: .whitespacesAndNewlines)
        let key = replacement?.isEmpty == false ? replacement : try credentials.read()
        guard let key, Self.validKey(key) else { throw OpenAITunnelError.missingKey }
        if replacement?.isEmpty == false { try credentials.write(key) }
        defaults.set(id, forKey: "openAITunnelID")
        defaults.set(true, forKey: "openAITunnelKeySaved")
        defaults.set(false, forKey: "openAITunnelNeedsKeyReview")
        credentialRejected = false
        update("Tunnel setup saved. Start the local connector, then start this tunnel.")
    }

    public func restoreAtLaunch() {
        guard enabled else { return }
        guard !defaults.bool(forKey: "openAITunnelNeedsKeyReview") else {
            update("The tunnel service rejected this key or its permissions. Update the runtime key before reconnecting."); return
        }
        do { try start() }
        catch OpenAITunnelError.keychain { scheduleRetry(reason: "Waiting for your login Keychain.") }
        catch { update(error.localizedDescription) }
    }

    public func start() throws {
        if running, !stopping { refreshStatus(); return }
        guard owned == nil else { throw OpenAITunnelError.alreadyRunning }
        guard configured else { throw OpenAITunnelError.missingKey }
        generation += 1
        retry?.cancel(); retry = nil
        failures = 0
        credentialRejected = false
        stopping = false
        do { try launch() }
        catch { releaseLocks(); throw error }
    }

    public func stop() {
        defaults.set(false, forKey: "openAITunnelEnabled")
        shutdown()
    }

    /// Quit preserves the saved choice for next launch, but stops this owned PID.
    public func shutdown() {
        stopping = true
        generation += 1
        retry?.cancel(); retry = nil
        timer?.invalidate(); timer = nil
        ready = false
        guard let owned else { running = false; releaseLocks(); update("OpenAI tunnel is stopped."); return }
        if isOwnedProcess(owned) || (child?.isRunning == true && child?.processIdentifier == owned.pid) {
            _ = kill(owned.pid, SIGTERM); update("Stopping OpenAI tunnel…")
        } else if originalProcessExited(owned) { exited(pid: owned.pid) }
        else { update("The running client changed identity unexpectedly. D-Bond will not stop an unverified process.") }
    }

    public func refreshStatus() {
        guard let owned, running, !stopping, !probePending, !credentialRejected else { return }
        guard isOwnedProcess(owned) else {
            if originalProcessExited(owned) { exited(pid: owned.pid) }
            else { ready = false; update("The running client changed identity unexpectedly. Its process and saved files have been preserved.") }
            return
        }
        let file = URL(fileURLWithPath: owned.runDirectory).appendingPathComponent("health.url")
        guard let value = try? String(contentsOf: file, encoding: .utf8), value.utf8.count < 2048,
              let url = Self.readinessURL(value.trimmingCharacters(in: .whitespacesAndNewlines)) else {
            ready = false; update("Tunnel client is starting; readiness is unverified."); return
        }
        probePending = true
        let expected = generation, pid = owned.pid
        let request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 3)
        session.dataTask(with: request) { [weak self] data, response, error in
            let body = data.flatMap { $0.count <= 4096 ? String(data: $0, encoding: .utf8) : nil }
            // Older clients return 200 for a timed-out MCP probe. Count only a
            // plain positive readiness response, and never claim a Dot test.
            let valid = error == nil && (response as? HTTPURLResponse)?.statusCode == 200
                && response?.url == url && body?.trimmingCharacters(in: .whitespacesAndNewlines) == "ready"
            Task { @MainActor in
                guard let self, self.generation == expected, self.owned?.pid == pid, !self.credentialRejected else { return }
                self.probePending = false
                self.ready = valid
                self.update(valid ? "Tunnel client reports ready. Verify the Dot connection to finish setup."
                    : "Tunnel client is running; readiness is unverified. Check the connector, tunnel permissions and network.")
            }
        }.resume()
    }

    static func readinessURL(_ value: String) -> URL? {
        guard let base = URLComponents(string: value), base.scheme == "http", base.host == "127.0.0.1",
              let port = base.port, (1...65535).contains(port), base.user == nil, base.password == nil,
              base.query == nil, base.fragment == nil, ["", "/"].contains(base.path) else { return nil }
        return base.url?.appendingPathComponent("readyz")
    }

    static func validKey(_ key: String) -> Bool {
        !key.isEmpty && key.utf8.count <= 16_384 && key.rangeOfCharacter(from: .whitespacesAndNewlines) == nil
    }

    static func arguments(tunnelID: String, run: URL, connectorDirectory: URL) -> [String] {
        ["run", "--control-plane.base-url", "https://api.openai.com",
         "--control-plane.tunnel-id", tunnelID,
         "--control-plane.api-key", "env:DBOND_TUNNEL_RUNTIME_KEY",
         "--mcp.server-url", "http://127.0.0.1:18773/mcp",
         "--mcp.extra-headers", "X-DBond-Tunnel: file:" + connectorDirectory.appendingPathComponent("tunnel-ingress.key").path,
         "--mcp.extra-headers", "X-DBond-Tunnel-ID: " + tunnelID,
         "--health.listen-addr", "127.0.0.1:0", "--health.url-file", run.appendingPathComponent("health.url").path,
         "--pid.file", run.appendingPathComponent("client.pid").path,
         "--profile-dir", run.appendingPathComponent("profiles").path,
         "--log.level", "warn", "--log.format", "json"]
    }

    static func environment(key: String, run: URL) -> [String: String] {
        var value = ["HOME": run.appendingPathComponent("home").path,
            "XDG_CONFIG_HOME": run.appendingPathComponent("home/.config").path,
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
            "DBOND_TUNNEL_RUNTIME_KEY": key]
        if let temp = ProcessInfo.processInfo.environment["TMPDIR"] { value["TMPDIR"] = temp }
        return value
    }

    private func launch() throws {
        try secureDirectory(directory)
        if controllerFD == -1 { controllerFD = try lockedFile("controller.lock") }
        if clientFD == -1 {
            do { clientFD = try lockedFile("client.lock") }
            catch OpenAITunnelError.alreadyRunning {
                // A surviving child holds its stdin lock across an app crash.
                // Adopt only its private exact PID/start-time/argv receipt.
                guard let saved = readReceipt(), saved.tunnelID == tunnelID, isOwnedProcess(saved) else { throw OpenAITunnelError.alreadyRunning }
                owned = saved
                activated()
                return
            }
        }
        let ingress = connectorDirectory.appendingPathComponent("tunnel-ingress.key")
        guard privateFile(ingress) else { throw OpenAITunnelError.missingHostKey }
        guard let key = try credentials.read(), Self.validKey(key) else { throw OpenAITunnelError.missingKey }
        guard let executable, FileManager.default.isExecutableFile(atPath: executable.path) else { throw OpenAITunnelError.missingClient }
        let run = directory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try secureDirectory(run)
        try secureDirectory(run.appendingPathComponent("profiles", isDirectory: true))
        try secureDirectory(run.appendingPathComponent("home", isDirectory: true))
        let arguments = Self.arguments(tunnelID: tunnelID, run: run, connectorDirectory: connectorDirectory)
        let process = Process()
        process.executableURL = executable.resolvingSymlinksInPath()
        process.arguments = arguments
        process.environment = Self.environment(key: key, run: run)
        process.currentDirectoryURL = run
        process.standardInput = FileHandle(fileDescriptor: clientFD, closeOnDealloc: false)
        let logs = Pipe()
        process.standardOutput = logs
        process.standardError = logs
        do { try process.run() }
        catch { try? FileManager.default.removeItem(at: run); throw OpenAITunnelError.launchFailed }
        child = process
        var info = proc_bsdinfo()
        let size = proc_pidinfo(process.processIdentifier, PROC_PIDTBSDINFO, 0, &info, Int32(MemoryLayout<proc_bsdinfo>.size))
        guard size == MemoryLayout<proc_bsdinfo>.size, let path = Self.processPath(process.processIdentifier) else {
            process.terminate(); throw OpenAITunnelError.launchFailed
        }
        let receipt = OwnedProcess(pid: process.processIdentifier, startSeconds: info.pbi_start_tvsec,
            startMicroseconds: info.pbi_start_tvusec, launchExecutable: executable.resolvingSymlinksInPath().path,
            processExecutable: path, tunnelID: tunnelID, runDirectory: run.path, arguments: arguments)
        do {
            let receiptURL = directory.appendingPathComponent("owned-process.json")
            try JSONEncoder().encode(receipt).write(to: receiptURL, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: receiptURL.path)
        } catch { process.terminate(); throw OpenAITunnelError.privateStorage }
        owned = receipt
        observeLogs(logs, pid: receipt.pid)
        activated()
    }

    private func activated() {
        running = true; ready = false; probePending = false; stopping = false
        startedAt = Date()
        defaults.set(true, forKey: "openAITunnelEnabled")
        let expected = generation, pid = owned!.pid
        watcher?.cancel()
        let source = DispatchSource.makeProcessSource(identifier: pid, eventMask: .exit, queue: .global(qos: .utility))
        source.setEventHandler { [weak self] in
            Task { @MainActor in
                guard let self, self.generation == expected || self.stopping else { return }
                self.exited(pid: pid)
            }
        }
        watcher = source
        source.resume()
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: pollInterval, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refreshStatus() }
        }
        update("Tunnel client is starting; readiness is unverified.")
        refreshStatus()
    }

    private func exited(pid: Int32) {
        guard let previous = owned, previous.pid == pid else { return }
        watcher?.cancel(); watcher = nil
        timer?.invalidate(); timer = nil
        owned = nil; child = nil; running = false; ready = false; probePending = false
        if clientFD != -1 { close(clientFD); clientFD = -1 }
        try? FileManager.default.removeItem(at: directory.appendingPathComponent("owned-process.json"))
        if safeRunDirectory(previous.runDirectory) { try? FileManager.default.removeItem(atPath: previous.runDirectory) }
        guard !stopping, enabled else { releaseLocks(); update("OpenAI tunnel is stopped."); return }
        guard !credentialRejected else {
            releaseLocks(); update("The tunnel service rejected this key or its permissions. Update the runtime key before reconnecting."); return
        }
        if Date().timeIntervalSince(startedAt) >= 60 { failures = 0 }
        scheduleRetry(reason: "Tunnel client interrupted.")
    }

    private func scheduleRetry(reason: String) {
        guard !retryDelays.isEmpty else {
            releaseLocks(); update("The tunnel client could not recover. Check its installation, key, permissions and network, then start it again."); return
        }
        let delay = retryDelays[min(failures, retryDelays.count - 1)]
        failures = min(failures + 1, retryDelays.count - 1)
        let expected = generation
        let item = DispatchWorkItem { [weak self] in
            Task { @MainActor in
                guard let self, self.generation == expected, !self.stopping, self.enabled, self.owned == nil else { return }
                self.retry = nil
                do { try self.launch() }
                catch OpenAITunnelError.keychain { self.scheduleRetry(reason: "Waiting for your login Keychain.") }
                catch { self.releaseLocks(); self.update(error.localizedDescription) }
            }
        }
        retry = item
        update("\(reason) Retrying in \(Int(ceil(delay))) seconds…")
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: item)
    }

    private func secureDirectory(_ url: URL) throws {
        let fileManager = FileManager.default
        if !fileManager.fileExists(atPath: url.path) {
            try fileManager.createDirectory(at: url, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        }
        var info = stat()
        guard lstat(url.path, &info) == 0, (info.st_mode & S_IFMT) == S_IFDIR, info.st_uid == getuid() else {
            throw OpenAITunnelError.privateStorage
        }
        try fileManager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: url.path)
    }

    private func lockedFile(_ name: String) throws -> Int32 {
        let fd = open(directory.appendingPathComponent(name).path, O_CREAT | O_RDWR | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw OpenAITunnelError.privateStorage }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_uid == getuid(), (info.st_mode & S_IFMT) == S_IFREG,
              (info.st_mode & 0o077) == 0 else { close(fd); throw OpenAITunnelError.privateStorage }
        guard flock(fd, LOCK_EX | LOCK_NB) == 0 else { close(fd); throw OpenAITunnelError.alreadyRunning }
        return fd
    }

    private func releaseLocks() {
        if clientFD != -1 { close(clientFD); clientFD = -1 }
        if controllerFD != -1 { close(controllerFD); controllerFD = -1 }
    }

    private func assertNoSurvivingClient() throws {
        let path = directory.appendingPathComponent("client.lock").path
        let fd = open(path, O_RDWR | O_NOFOLLOW | O_CLOEXEC)
        if fd < 0 {
            if errno == ENOENT { return }
            throw OpenAITunnelError.privateStorage
        }
        defer { close(fd) }
        guard flock(fd, LOCK_EX | LOCK_NB) == 0 else { throw OpenAITunnelError.alreadyRunning }
    }

    nonisolated static func isCredentialFailure(_ line: Data) -> Bool {
        guard let row = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else { return false }
        let text = row.values.compactMap { $0 as? String }.joined(separator: " ").lowercased()
        let controlPlane = text.contains("control") || text.contains("poll") || text.contains("api.openai.com")
        let rejected = text.contains("unauthorized") || text.contains("invalid_api_key") || text.contains("authentication_error")
            || text.contains("403") || text.contains("401") || text.contains("permission")
        return controlPlane && rejected
    }

    private func observeLogs(_ pipe: Pipe, pid: Int32) {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            var buffer = Data()
            while true {
                let data = pipe.fileHandleForReading.availableData
                if data.isEmpty { break }
                buffer.append(data)
                while let end = buffer.firstIndex(of: 10) {
                    let line = Data(buffer.prefix(upTo: end))
                    buffer.removeSubrange(...end)
                    guard line.count <= 16_384, Self.isCredentialFailure(line) else { continue }
                    Task { @MainActor in
                        guard let self, self.owned?.pid == pid, !self.stopping else { return }
                        self.credentialRejected = true
                        self.ready = false
                        self.defaults.set(true, forKey: "openAITunnelNeedsKeyReview")
                        self.update("The tunnel service rejected this key or its permissions. Update the runtime key before reconnecting.")
                    }
                }
                if buffer.count > 16_384 { buffer.removeAll(keepingCapacity: true) }
            }
        }
    }

    private func privateFile(_ url: URL) -> Bool {
        var info = stat()
        return lstat(url.path, &info) == 0 && info.st_uid == getuid()
            && (info.st_mode & S_IFMT) == S_IFREG && (info.st_mode & 0o077) == 0 && info.st_size <= 16_384
    }

    private func readReceipt() -> OwnedProcess? {
        let path = directory.appendingPathComponent("owned-process.json")
        guard privateFile(path), let data = try? Data(contentsOf: path) else { return nil }
        return try? JSONDecoder().decode(OwnedProcess.self, from: data)
    }

    private func safeRunDirectory(_ path: String) -> Bool {
        let url = URL(fileURLWithPath: path)
        var info = stat()
        return UUID(uuidString: url.lastPathComponent) != nil &&
            url.deletingLastPathComponent().resolvingSymlinksInPath().path == directory.resolvingSymlinksInPath().path &&
            lstat(path, &info) == 0 && (info.st_mode & S_IFMT) == S_IFDIR && info.st_uid == getuid()
    }

    private func isOwnedProcess(_ receipt: OwnedProcess) -> Bool {
        guard receipt.pid > 1, receipt.tunnelID == tunnelID, safeRunDirectory(receipt.runDirectory),
              receipt.launchExecutable.hasPrefix("/"),
              let currentExecutable = Self.processPath(receipt.pid),
              receipt.arguments == Self.arguments(tunnelID: tunnelID, run: URL(fileURLWithPath: receipt.runDirectory), connectorDirectory: connectorDirectory) else { return false }
        var info = proc_bsdinfo()
        guard proc_pidinfo(receipt.pid, PROC_PIDTBSDINFO, 0, &info, Int32(MemoryLayout<proc_bsdinfo>.size)) == MemoryLayout<proc_bsdinfo>.size,
              info.pbi_start_tvsec == receipt.startSeconds, info.pbi_start_tvusec == receipt.startMicroseconds,
              let arguments = Self.processArguments(receipt.pid), arguments.count >= receipt.arguments.count else { return false }
        guard arguments.suffix(receipt.arguments.count).elementsEqual(receipt.arguments) else { return false }
        // macOS command-line launchers can exec another interpreter after
        // posix_spawn returns. Its argv still identifies the original script.
        // A Homebrew relink cannot change the stored process and birth time.
        return currentExecutable == receipt.processExecutable ||
            arguments.dropLast(receipt.arguments.count).contains(receipt.launchExecutable)
    }

    private func originalProcessExited(_ receipt: OwnedProcess) -> Bool {
        var info = proc_bsdinfo()
        if proc_pidinfo(receipt.pid, PROC_PIDTBSDINFO, 0, &info, Int32(MemoryLayout<proc_bsdinfo>.size)) == MemoryLayout<proc_bsdinfo>.size {
            return info.pbi_start_tvsec != receipt.startSeconds || info.pbi_start_tvusec != receipt.startMicroseconds
        }
        return kill(receipt.pid, 0) != 0 && errno == ESRCH
    }

    private static func processPath(_ pid: Int32) -> String? {
        // PROC_PIDPATHINFO_MAXSIZE is a C expression macro, unavailable in Swift.
        var bytes = [CChar](repeating: 0, count: 4 * Int(MAXPATHLEN))
        guard proc_pidpath(pid, &bytes, UInt32(bytes.count)) > 0 else { return nil }
        return String(cString: bytes)
    }

    private static func processArguments(_ pid: Int32) -> [String]? {
        var mib: [Int32] = [CTL_KERN, KERN_PROCARGS2, pid], size = 0
        guard sysctl(&mib, UInt32(mib.count), nil, &size, nil, 0) == 0, size > 4, size <= 1_048_576 else { return nil }
        var bytes = [UInt8](repeating: 0, count: size)
        guard sysctl(&mib, UInt32(mib.count), &bytes, &size, nil, 0) == 0 else { return nil }
        let count = bytes.withUnsafeBytes { $0.loadUnaligned(as: Int32.self) }
        guard count > 0, count < 4096 else { return nil }
        var index = MemoryLayout<Int32>.size
        while index < size && bytes[index] != 0 { index += 1 }
        while index < size && bytes[index] == 0 { index += 1 }
        var result: [String] = []
        for _ in 0..<count {
            let start = index
            while index < size && bytes[index] != 0 { index += 1 }
            guard index < size, let value = String(bytes: bytes[start..<index], encoding: .utf8) else { return nil }
            result.append(value); index += 1
        }
        return result
    }

    private func update(_ message: String) {
        guard state != message else { return }
        state = message
        NotificationCenter.default.post(name: Self.changed, object: self)
    }
}
