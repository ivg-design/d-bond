import Foundation

@main
struct IngressLifecycleFixture {
    @MainActor
    static func wait(_ condition: () -> Bool, seconds: TimeInterval = 4) async {
        let deadline = Date().addingTimeInterval(seconds)
        while !condition() && Date() < deadline { try? await Task.sleep(nanoseconds: 10_000_000) }
        precondition(condition(), "Ingress lifecycle fixture timed out")
    }

    @MainActor
    static func main() async throws {
        let root = URL(fileURLWithPath: ProcessInfo.processInfo.environment["DBOND_INGRESS_FIXTURE_DIR"]!)
        let launcher = root.appendingPathComponent("fixture-helper")
        try """
        #!/usr/bin/python3
        import json,os,signal,socket,sys
        count=os.environ['QA_COUNT']
        existing=open(count).read().splitlines() if os.path.exists(count) else []
        sock=socket.socket();sock.bind(('127.0.0.1',0))
        with open(count,'a') as dst: dst.write(json.dumps({'args':sys.argv[1:],'port':sock.getsockname()[1],'parent':os.getppid()})+'\\n')
        behavior=os.environ.get('QA_BEHAVIOR','first-fails')
        if behavior=='invalid':sys.exit(64)
        if behavior=='always-fails' or (behavior=='first-fails' and not existing):sys.exit(75)
        print('{"state":"restricted_proxy_ready"}',flush=True)
        signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
        signal.pause()
        """.write(to: launcher, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: launcher.path)

        func lines(_ file: URL) -> [[String: Any]] {
            (try? String(contentsOf: file, encoding: .utf8).split(separator: "\n").compactMap {
                try JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any]
            }) ?? []
        }
        let suite = "design.ivg.dbond.fixture.ingress." + UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let count = root.appendingPathComponent("recovery.jsonl")
        let environment = ["HOME": root.appendingPathComponent("home").path, "QA_COUNT": count.path, "QA_BEHAVIOR": "first-fails"]
        let controller = DotIngressController(defaults: defaults, launcher: launcher, environment: environment, retryDelays: [0.03, 0.03])
        try controller.start(mode: "https", hostname: "connector.example.org", token: nil)
        await wait { lines(count).count == 2 && controller.running }
        let expected = ["--mode", "https", "--hostname", "connector.example.org"]
        precondition(lines(count).allSatisfy { $0["args"] as? [String] == expected })
        precondition(lines(count).allSatisfy { ![18769, 18770].contains($0["port"] as? Int ?? 0) })
        do { try controller.start(mode: "https", hostname: "other.example.org", token: nil); preconditionFailure("duplicate start accepted") }
        catch { }
        precondition(lines(count).count == 2)
        controller.stop()
        await wait { !controller.running }
        try? await Task.sleep(nanoseconds: 150_000_000)
        precondition(lines(count).count == 2 && !controller.enabled)
        controller.restoreAtLaunch()
        precondition(lines(count).count == 2)

        let invalidCount = root.appendingPathComponent("invalid.jsonl")
        let invalid = DotIngressController(defaults: defaults, launcher: launcher,
            environment: ["HOME": environment["HOME"]!, "QA_COUNT": invalidCount.path, "QA_BEHAVIOR": "invalid"], retryDelays: [0.01])
        try invalid.start(mode: "https", hostname: "connector.example.org", token: nil)
        await wait { lines(invalidCount).count == 1 && !invalid.running }
        invalid.restoreAtLaunch()
        try? await Task.sleep(nanoseconds: 120_000_000)
        precondition(lines(invalidCount).count == 1 && defaults.bool(forKey: "dotIngressNeedsAction"))
        invalid.stop()

        let exhaustedCount = root.appendingPathComponent("budget.jsonl")
        let exhausted = DotIngressController(defaults: defaults, launcher: launcher,
            environment: ["HOME": environment["HOME"]!, "QA_COUNT": exhaustedCount.path, "QA_BEHAVIOR": "always-fails"], retryDelays: [0.02, 0.02])
        try exhausted.start(mode: "https", hostname: "connector.example.org", token: nil)
        await wait { lines(exhaustedCount).count == 3 && !exhausted.running && exhausted.state.contains("several attempts") }
        try? await Task.sleep(nanoseconds: 120_000_000)
        precondition(lines(exhaustedCount).count == 3)
        exhausted.stop()
        print("PASS: saved route recovery, no duplicate, manual stop, terminal config, bounded retries; isolated HOME and ephemeral ports")
    }
}
