import AppKit
import Foundation
import Security

/// Owns optional ingress only. Existing HTTPS never requires a cloud account.
@MainActor
final class DotIngressController {
    static let shared = DotIngressController()
    static let changed = Notification.Name("D-Bond.DotIngressChanged")
    private(set) var state = "Local only. Dot needs a reachable HTTPS address."
    private(set) var running = false
    private(set) var verified = false
    private var child: Process?
    private var requestedStop = false
    private let defaults: UserDefaults
    private let launcherOverride: URL?
    private let environmentOverride: [String: String]?
    private let retryDelays: [TimeInterval]
    private var retry: DispatchWorkItem?
    private var generation = 0
    private var failures = 0
    private var startedAt = Date()
    private let credentialService = "design.ivg.dbond.dot-ingress"
    var mode: String { defaults.string(forKey: "dotIngressMode") ?? "cloudflare" }
    var hostname: String { defaults.string(forKey: "dotIngressHostname") ?? "" }
    var enabled: Bool { defaults.bool(forKey: "dotIngressEnabled") }
    var publicOrigin: String { hostname.isEmpty ? "" : "https://" + hostname }

    init(defaults: UserDefaults = .standard, launcher: URL? = nil,
         environment: [String: String]? = nil, retryDelays: [TimeInterval] = [1, 2, 4]) {
        self.defaults = defaults
        self.launcherOverride = launcher
        self.environmentOverride = environment
        self.retryDelays = retryDelays
    }

    func restoreAtLaunch() {
        guard enabled else { return }
        guard !defaults.bool(forKey: "dotIngressNeedsAction") else {
            update("Ingress needs attention. Check its saved setup and reconnect explicitly."); return
        }
        do { try start(mode: mode, hostname: hostname, token: nil) }
        catch { update("Saved ingress could not start. Open Public HTTPS Setup to reconnect.") }
    }

    func shutdown() {
        requestedStop = true
        generation += 1
        retry?.cancel(); retry = nil
        if let child, child.isRunning { child.terminate() }
        // Keep the owned process identity until its exit. A quick Stop/Start
        // must not create a second helper before the old proxy releases its port.
        running = child?.isRunning == true
        verified = false
    }

    func stop() {
        defaults.set(false, forKey: "dotIngressEnabled")
        shutdown()
        update(running ? "Stopping ingress…" : "Stopped. Dot is available only through any separately configured HTTPS route.")
    }

    func start(mode: String, hostname raw: String, token: String?) throws {
        guard child?.isRunning != true else {
            throw IngressError.message("Wait for the current ingress to stop before starting another.")
        }
        generation += 1
        retry?.cancel(); retry = nil
        failures = 0
        defaults.set(false, forKey: "dotIngressNeedsAction")
        try launch(mode: mode, hostname: raw, token: token)
    }

    private func launch(mode: String, hostname raw: String, token: String?) throws {
        guard child == nil || child?.isRunning == false else {
            throw IngressError.message("Stop the current ingress before changing its settings.")
        }
        let host = raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard ["https", "cloudflare"].contains(mode),
              host.range(of: "^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]{2,63}$", options: .regularExpression) != nil,
              host.count <= 253 else { throw IngressError.message("Enter your public DNS hostname without https:// or a path.") }
        var capability: String?
        if mode == "cloudflare" {
            capability = token?.trimmingCharacters(in: .whitespacesAndNewlines)
            if capability?.isEmpty != false { capability = try keychainToken() }
            guard let value = capability, !value.isEmpty, value.count <= 16_384,
                  value.rangeOfCharacter(from: .whitespacesAndNewlines) == nil else {
                throw IngressError.message("Paste the tunnel token from your Cloudflare dashboard.")
            }
            if token?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false { try saveToken(value) }
        }
        #if DBOND_APP_BUNDLE
        let launcher = Bundle.main.resourceURL?.appendingPathComponent("dot-ingress/ingress")
        #else
        let source = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("adapters/dot-ingress/ingress")
        let launcher = Bundle.main.bundleURL.pathExtension == "app"
            ? Bundle.main.resourceURL?.appendingPathComponent("dot-ingress/ingress") : source
        #endif
        guard let launcher = launcherOverride ?? launcher, FileManager.default.isExecutableFile(atPath: launcher.path) else {
            throw IngressError.message("The ingress helper is missing from this D-Bond build.")
        }
        let process = Process(), output = Pipe()
        process.executableURL = launcher
        process.arguments = ["--mode", mode, "--hostname", host]
        var environment = environmentOverride ?? ProcessInfo.processInfo.environment
        environment["DBOND_INGRESS_PARENT_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        if let capability { environment["TUNNEL_TOKEN"] = capability }
        else { environment.removeValue(forKey: "TUNNEL_TOKEN") }
        process.environment = environment
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        requestedStop = false
        verified = false
        process.terminationHandler = { [weak self] ended in
            Task { @MainActor in
                guard let self, self.child === ended else { return }
                self.child = nil
                self.running = false
                self.verified = false
                if self.requestedStop {
                    self.update("Stopped. Dot is available only through any separately configured HTTPS route.")
                } else if ended.terminationReason == .exit && ended.terminationStatus == 64 {
                    self.defaults.set(true, forKey: "dotIngressNeedsAction")
                    self.update("Ingress setup needs attention. Check the hostname, token and port 18770, then reconnect explicitly.")
                } else {
                    self.scheduleRecovery()
                }
            }
        }
        try process.run()
        child = process
        startedAt = Date()
        running = true
        defaults.set(mode, forKey: "dotIngressMode")
        defaults.set(host, forKey: "dotIngressHostname")
        defaults.set(true, forKey: "dotIngressEnabled")
        update("Starting restricted ingress…")
        DispatchQueue.global(qos: .utility).async { [weak self] in
            var buffer = Data()
            while true {
                let data = output.fileHandleForReading.availableData
                if data.isEmpty { break }
                buffer.append(data)
                while let end = buffer.firstIndex(of: 10) {
                    let line = buffer.prefix(upTo: end)
                    buffer.removeSubrange(...end)
                    guard let row = try? JSONSerialization.jsonObject(with: line) as? [String: String],
                          let state = row["state"] else { continue }
                    Task { @MainActor in
                        guard let self, self.child === process else { return }
                        switch state {
                        case "restricted_proxy_ready": self.update("Restricted local ingress is ready at 127.0.0.1:18770. Public reachability has not been verified.")
                        case "preparing_official_tunnel": self.update("Preparing the checksum-verified official Cloudflare helper…")
                        case "tunnel_process_started": self.update("Cloudflare helper started. Verify HTTPS to check DNS, routing and the Dot connector.")
                        case "tunnel_reconnecting": self.verified = false; self.update("Cloudflare helper is reconnecting. Public reachability is unverified.")
                        case "ingress_failed": self.verified = false; self.update("Ingress could not start. Check setup and reconnect.")
                        default: break
                        }
                    }
                }
                if buffer.count > 4096 { break }
            }
        }
    }

    private func scheduleRecovery() {
        guard enabled, !requestedStop, retry == nil else { return }
        if Date().timeIntervalSince(startedAt) >= 60 { failures = 0 }
        guard failures < retryDelays.count else {
            update("Ingress could not recover after several attempts. Check the network and setup, then reconnect."); return
        }
        let delay = retryDelays[failures]
        failures += 1
        let expected = generation, savedMode = mode, savedHost = hostname
        let item = DispatchWorkItem { [weak self] in
            Task { @MainActor in
                guard let self, self.generation == expected, self.enabled, !self.requestedStop,
                      self.child == nil, self.mode == savedMode, self.hostname == savedHost else { return }
                self.retry = nil
                do { try self.launch(mode: savedMode, hostname: savedHost, token: nil) }
                catch {
                    self.defaults.set(true, forKey: "dotIngressNeedsAction")
                    self.update("Saved ingress could not restart. Check setup and reconnect explicitly.")
                }
            }
        }
        retry = item
        update("Ingress interrupted. Retrying the same saved route in \(Int(ceil(delay))) seconds…")
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: item)
    }

    func verifyHTTPS() {
        guard running, let url = URL(string: publicOrigin + "/.well-known/oauth-authorization-server") else {
            update("Start ingress before verifying HTTPS."); return
        }
        let origin = publicOrigin
        let request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 15)
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            let row = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            let valid = error == nil && (response as? HTTPURLResponse)?.statusCode == 200
                && response?.url == url && row?["issuer"] as? String == origin
            Task { @MainActor in
                guard let self, self.publicOrigin == origin else { return }
                self.verified = valid
                self.update(valid ? "HTTPS reaches this Dot connector. Continue with Connect Dot and its OAuth setup."
                    : "HTTPS is not verified. Check DNS, your tunnel route to port 18770, and the running Dot connector. Local-only access cannot connect your Dot.")
            }
        }.resume()
    }

    private func update(_ message: String) {
        state = message
        NotificationCenter.default.post(name: Self.changed, object: self)
    }
    private var query: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: credentialService,
         kSecAttrAccount as String: "named-tunnel"]
    }
    private func keychainToken() throws -> String? {
        var lookup = query
        lookup[kSecReturnData as String] = true
        lookup[kSecMatchLimit as String] = kSecMatchLimitOne
        var value: CFTypeRef?
        let status = SecItemCopyMatching(lookup as CFDictionary, &value)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = value as? Data else {
            throw IngressError.message("Unlock your login Keychain or paste the tunnel token again.")
        }
        return String(data: data, encoding: .utf8)
    }
    private func saveToken(_ token: String) throws {
        let values: [String: Any] = [kSecValueData as String: Data(token.utf8),
                                    kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly]
        var status = SecItemUpdate(query as CFDictionary, values as CFDictionary)
        if status == errSecItemNotFound { status = SecItemAdd(query.merging(values) { _, value in value } as CFDictionary, nil) }
        guard status == errSecSuccess else { throw IngressError.message("The tunnel token could not be saved in your login Keychain.") }
    }
}

private enum IngressError: LocalizedError {
    case message(String)
    var errorDescription: String? { if case let .message(text) = self { return text }; return nil }
}
