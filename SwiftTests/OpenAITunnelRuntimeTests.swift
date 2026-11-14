import XCTest
import Darwin
@testable import DBondCore

@MainActor
private final class MemoryTunnelCredentials: OpenAITunnelCredentialStore {
    var value: String?
    var writes = 0
    var reads = 0
    var locked = false
    func read() throws -> String? {
        reads += 1
        if locked { throw OpenAITunnelError.keychain }
        return value
    }
    func write(_ value: String) throws { self.value = value; writes += 1 }
}

@MainActor
final class OpenAITunnelRuntimeTests: XCTestCase {
    private let id = "tunnel_" + String(repeating: "a", count: 32)

    private struct Fixture {
        let root: URL
        let runtime: OpenAITunnelRuntime
        let defaults: UserDefaults
        let credentials: MemoryTunnelCredentials
        let executable: URL
        let state: URL
        let host: URL
    }

    private func fixture(retryDelays: [TimeInterval] = [0.02, 0.03]) throws -> Fixture {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("dbond-tunnel-test-" + UUID().uuidString, isDirectory: true)
        let host = root.appendingPathComponent("host", isDirectory: true)
        try FileManager.default.createDirectory(at: host, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let key = host.appendingPathComponent("tunnel-ingress.key")
        try Data("fixture-ingress".utf8).write(to: key)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: key.path)
        let executable = root.appendingPathComponent("fake-tunnel")
        let script = #"""
        #!/usr/bin/python3
        import http.server,json,os,pathlib,sys,time
        root=pathlib.Path(__file__).parent
        args=sys.argv[1:]
        with (root/'calls.jsonl').open('a') as f:
            f.write(json.dumps(dict(args=args,key_present=bool(os.environ.get('DBOND_TUNNEL_RUNTIME_KEY')),ambient=bool(os.environ.get('OPENAI_API_KEY') or os.environ.get('TUNNEL_CLIENT_PROFILE'))))+'\n')
        until=root/'recover-at'
        if until.exists() and time.time()<float(until.read_text()):sys.exit(1)
        if (root/'reject-key').exists():
            print(json.dumps(dict(msg='control plane poll failed',error='HTTP 401 unauthorized')),flush=True)
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body=(root/'ready-body').read_text() if (root/'ready-body').exists() else 'ready'
                self.send_response(200);self.end_headers();self.wfile.write(body.encode())
            def log_message(self,*a):pass
        server=http.server.HTTPServer(('127.0.0.1',0),Handler)
        pathlib.Path(args[args.index('--health.url-file')+1]).write_text('http://127.0.0.1:'+str(server.server_port))
        pathlib.Path(args[args.index('--pid.file')+1]).write_text(str(os.getpid()))
        server.serve_forever(poll_interval=.02)
        """#
        try script.write(to: executable, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: executable.path)
        let defaults = UserDefaults(suiteName: root.lastPathComponent)!
        let credentials = MemoryTunnelCredentials()
        let state = root.appendingPathComponent("runtime", isDirectory: true)
        let runtime = OpenAITunnelRuntime(defaults: defaults, stateDirectory: state,
            connectorStateDirectory: host, executable: executable, credentialStore: credentials,
            retryDelays: retryDelays, pollInterval: 0.03)
        return Fixture(root: root, runtime: runtime, defaults: defaults, credentials: credentials,
                       executable: executable, state: state, host: host)
    }

    private func eventually(timeout: TimeInterval = 4, _ condition: () -> Bool) async -> Bool {
        let limit = Date().addingTimeInterval(timeout)
        while Date() < limit {
            if condition() { return true }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        return condition()
    }

    private func cleanup(_ fixture: Fixture) async {
        fixture.runtime.stop()
        _ = await eventually { !fixture.runtime.running }
        fixture.defaults.removePersistentDomain(forName: fixture.root.lastPathComponent)
        try? FileManager.default.removeItem(at: fixture.root)
    }

    func testLaunchContractKeepsSecretsOutOfArgumentsAndAmbientProfiles() {
        let run = URL(fileURLWithPath: "/private/runtime/run", isDirectory: true)
        let args = OpenAITunnelRuntime.arguments(tunnelID: id, run: run, connectorDirectory: URL(fileURLWithPath: "/private/host with spaces", isDirectory: true))
        let env = OpenAITunnelRuntime.environment(key: "fixture-secret", run: run)
        XCTAssertFalse(args.contains(where: { $0.contains("fixture-secret") }))
        XCTAssertTrue(args.contains("env:DBOND_TUNNEL_RUNTIME_KEY"))
        XCTAssertTrue(args.contains("X-DBond-Tunnel: file:/private/host with spaces/tunnel-ingress.key"))
        XCTAssertTrue(args.contains("X-DBond-Tunnel-ID: " + id))
        XCTAssertTrue(args.contains("127.0.0.1:0"))
        XCTAssertTrue(args.contains(run.appendingPathComponent("profiles").path))
        XCTAssertEqual(env["DBOND_TUNNEL_RUNTIME_KEY"], "fixture-secret")
        XCTAssertNil(env["OPENAI_API_KEY"])
        XCTAssertNil(env["TUNNEL_CLIENT_PROFILE"])
        XCTAssertNil(env["CONTROL_PLANE_API_KEY"])
    }

    func testCanonicalIDsAndLocalReadinessValidation() {
        XCTAssertTrue(OpenAITunnelRuntime.validTunnelID(id))
        XCTAssertTrue(OpenAITunnelRuntime.validTunnelID("tunnel_ab12_" + String(repeating: "f", count: 32)))
        XCTAssertFalse(OpenAITunnelRuntime.validTunnelID("Vollna"))
        XCTAssertFalse(OpenAITunnelRuntime.validTunnelID(id + "\n"))
        XCTAssertEqual(OpenAITunnelRuntime.readinessURL("http://127.0.0.1:1234")?.path, "/readyz")
        for value in ["https://127.0.0.1:1", "http://evil.example:1", "http://127.0.0.1:1/mcp", "http://user@127.0.0.1:1", "http://127.0.0.1:1?q=x"] {
            XCTAssertNil(OpenAITunnelRuntime.readinessURL(value))
        }
    }

    func testSavedKeyReuseReadyStopAndNoDuplicateProcess() async throws {
        let f = try fixture()
        try f.runtime.configure(tunnelID: id, apiKey: "fixture-key")
        try f.runtime.configure(tunnelID: id, apiKey: nil)
        XCTAssertEqual(f.credentials.writes, 1)
        try f.runtime.start()
        let healthy = await eventually { f.runtime.ready }
        XCTAssertTrue(healthy, f.runtime.state)
        let first = f.runtime.processIdentifier
        try f.runtime.start()
        XCTAssertEqual(f.runtime.processIdentifier, first)
        let other = OpenAITunnelRuntime(defaults: f.defaults, stateDirectory: f.state,
            connectorStateDirectory: f.host, executable: f.executable, credentialStore: f.credentials)
        XCTAssertThrowsError(try other.start())
        other.shutdown()
        XCTAssertTrue(f.runtime.running)
        let log = try String(contentsOf: f.root.appendingPathComponent("calls.jsonl"), encoding: .utf8)
        XCTAssertEqual(log.split(separator: "\n").count, 1)
        XCTAssertFalse(log.contains("fixture-key"))
        XCTAssertTrue(log.contains("\"ambient\": false"))
        f.runtime.shutdown()
        let stopped = await eventually { !f.runtime.running }
        XCTAssertTrue(stopped)
        XCTAssertTrue(f.runtime.enabled, "Quit retains the saved next-launch choice")
        f.runtime.restoreAtLaunch()
        let restored = await eventually { f.runtime.ready }
        XCTAssertTrue(restored, f.runtime.state)
        XCTAssertEqual(f.credentials.writes, 1)
        await cleanup(f)
    }

    func testAppCrashAdoptsOneSurvivorAfterClientRelink() async throws {
        // Launch this test alone in a second process. Killing its controller
        // closes the parent's descriptors exactly as an app crash would.
        let key = "DBOND_XCTEST_CRASH_ROOT"
        if let path = ProcessInfo.processInfo.environment[key] {
            let root = URL(fileURLWithPath: path, isDirectory: true)
            let credentials = MemoryTunnelCredentials()
            let runtime = OpenAITunnelRuntime(defaults: UserDefaults(suiteName: root.lastPathComponent)!,
                stateDirectory: root.appendingPathComponent("runtime"),
                connectorStateDirectory: root.appendingPathComponent("host"),
                executable: root.appendingPathComponent("client-link"), credentialStore: credentials,
                pollInterval: 0.03)
            try runtime.configure(tunnelID: id, apiKey: "fixture-key")
            try runtime.start()
            guard await eventually({ runtime.ready }), let pid = runtime.processIdentifier else {
                XCTFail(runtime.state); return
            }
            try String(pid).write(to: root.appendingPathComponent("controller-ready"), atomically: true, encoding: .utf8)
            try await Task.sleep(nanoseconds: 30_000_000_000)
            runtime.stop()
            return
        }

        let f = try fixture()
        let link = f.root.appendingPathComponent("client-link")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: f.executable)
        let controller = Process()
        controller.executableURL = URL(fileURLWithPath: "/usr/bin/xcrun")
        controller.arguments = ["xctest", "-XCTest", "DBondCoreTests.OpenAITunnelRuntimeTests/testAppCrashAdoptsOneSurvivorAfterClientRelink", Bundle(for: type(of: self)).bundlePath]
        controller.environment = [key: f.root.path, "PATH": "/usr/bin:/bin"]
        let report = f.root.appendingPathComponent("controller-output")
        FileManager.default.createFile(atPath: report.path, contents: nil)
        let output = try FileHandle(forWritingTo: report)
        controller.standardOutput = output
        controller.standardError = output
        try controller.run()
        defer { if controller.isRunning { controller.terminate() } }
        let signal = f.root.appendingPathComponent("controller-ready")
        guard await eventually(timeout: 10, { FileManager.default.fileExists(atPath: signal.path) }),
              let pid = Int32(try String(contentsOf: signal, encoding: .utf8)) else {
            let details = (try? String(contentsOf: report, encoding: .utf8)) ?? ""
            XCTFail("Second test controller did not signal readiness: " + details); return
        }
        kill(controller.processIdentifier, SIGKILL)
        controller.waitUntilExit()
        XCTAssertEqual(kill(pid, 0), 0)
        let upgraded = f.root.appendingPathComponent("upgraded-client")
        try FileManager.default.copyItem(at: f.executable, to: upgraded)
        try FileManager.default.removeItem(at: link)
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: upgraded)
        // A second controller uses the saved preferences, same state directory,
        // and new client version. An adopted client needs no Keychain read.
        f.defaults.set(id, forKey: "openAITunnelID")
        f.defaults.set(true, forKey: "openAITunnelKeySaved")
        f.defaults.set(true, forKey: "openAITunnelEnabled")
        let restored = OpenAITunnelRuntime(defaults: f.defaults, stateDirectory: f.state,
            connectorStateDirectory: f.host, executable: link, credentialStore: f.credentials, pollInterval: 0.03)
        restored.restoreAtLaunch()
        let ready = await eventually { restored.ready }
        XCTAssertTrue(ready, restored.state)
        XCTAssertEqual(restored.processIdentifier, pid)
        XCTAssertEqual(f.credentials.reads, 0)
        XCTAssertEqual(try String(contentsOf: f.root.appendingPathComponent("calls.jsonl"), encoding: .utf8).split(separator: "\n").count, 1)
        restored.stop()
        let exited = await eventually { kill(pid, 0) != 0 && errno == ESRCH }
        if !exited { kill(pid, SIGTERM) }
        XCTAssertTrue(exited)
        await cleanup(f)
    }

    func testLoginRestoresWhenKeychainUnlocks() async throws {
        let f = try fixture()
        try f.runtime.configure(tunnelID: id, apiKey: "fixture-key")
        f.defaults.set(true, forKey: "openAITunnelEnabled")
        f.credentials.locked = true
        f.runtime.restoreAtLaunch()
        XCTAssertTrue(f.runtime.state.contains("Keychain"))
        XCTAssertFalse(f.runtime.running)
        f.credentials.locked = false
        let restored = await eventually { f.runtime.ready }
        XCTAssertTrue(restored, f.runtime.state)
        XCTAssertEqual(f.credentials.writes, 1)
        await cleanup(f)
    }

    func testTimeoutReadyBodyDoesNotClaimReadiness() async throws {
        let f = try fixture()
        try "ready (mcp startup probe timed out: fixture)".write(to: f.root.appendingPathComponent("ready-body"), atomically: true, encoding: .utf8)
        try f.runtime.configure(tunnelID: id, apiKey: "fixture-key")
        try f.runtime.start()
        try await Task.sleep(nanoseconds: 400_000_000)
        XCTAssertTrue(f.runtime.running)
        XCTAssertFalse(f.runtime.ready)
        try "ready".write(to: f.root.appendingPathComponent("ready-body"), atomically: true, encoding: .utf8)
        let recovered = await eventually { f.runtime.ready }
        XCTAssertTrue(recovered, f.runtime.state)
        await cleanup(f)
    }

    func testOutageLongerThanSevenSecondsRecoversFromSavedSetup() async throws {
        let f = try fixture(retryDelays: [0.1, 0.2, 0.4, 0.8, 1])
        try String(Date().addingTimeInterval(8).timeIntervalSince1970).write(to: f.root.appendingPathComponent("recover-at"), atomically: true, encoding: .utf8)
        try f.runtime.configure(tunnelID: id, apiKey: "fixture-key")
        try f.runtime.start()
        let recovered = await eventually(timeout: 13) { f.runtime.ready }
        XCTAssertTrue(recovered, f.runtime.state)
        XCTAssertEqual(f.credentials.writes, 1)
        XCTAssertTrue(f.runtime.enabled)
        await cleanup(f)
    }

    func testCredentialRejectionRemainsActionableInsteadOfClaimingReady() async throws {
        let f = try fixture()
        try Data().write(to: f.root.appendingPathComponent("reject-key"))
        try f.runtime.configure(tunnelID: id, apiKey: "fixture-key")
        try f.runtime.start()
        let rejected = await eventually { f.runtime.state.contains("rejected") }
        XCTAssertTrue(rejected, f.runtime.state)
        XCTAssertFalse(f.runtime.ready)
        XCTAssertTrue(f.defaults.bool(forKey: "openAITunnelNeedsKeyReview"))
        await cleanup(f)
    }
}
