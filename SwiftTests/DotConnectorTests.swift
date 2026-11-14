import XCTest
import Darwin
@testable import DBondCore

final class DotConnectorTests: XCTestCase {
    func testSetupURLsAndIdentityGate() throws {
        XCTAssertTrue(DotConnector.validSetup(publicURL: "https://connector.example.com", redirectURI: "https://chatgpt.com/oauth/callback"))
        for origin in ["http://example.com", "https://user:pass@example.com", "https://example.com/path", "https://example.com?q=1"] {
            XCTAssertFalse(DotConnector.validSetup(publicURL: origin, redirectURI: "https://chatgpt.com/callback"))
        }
        for json in [
            #"{"configured":true,"running":false,"subscribed":true,"identity":{"connected":true,"active_subscriptions":1}}"#,
            #"{"configured":true,"running":true,"subscribed":false,"identity":{"connected":true,"active_subscriptions":1}}"#,
            #"{"configured":true,"running":true,"subscribed":true,"identity":{"connected":true,"active_subscriptions":2}}"#
        ] {
            XCTAssertFalse(try JSONDecoder().decode(DotConnectorStatus.self, from: Data(json.utf8)).canSelectOwnedRoute)
        }
        let ready = #"{"configured":true,"running":true,"subscribed":true,"identity":{"connected":true,"active_subscriptions":1}}"#
        XCTAssertTrue(try JSONDecoder().decode(DotConnectorStatus.self, from: Data(ready.utf8)).canSelectOwnedRoute)
    }

    private func fixture() throws -> (URL, DotConnector, UserDefaults) {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let executable = root.appendingPathComponent("connector")
        let script = #"""
        #!/usr/bin/python3
        import argparse,json,os,pathlib,signal,sys,time
        p=argparse.ArgumentParser();p.add_argument('--state-dir');p.add_argument('action');p.add_argument('--public-url');p.add_argument('--redirect-uri');a=p.parse_args()
        root=pathlib.Path(a.state_dir);root.mkdir(exist_ok=True)
        with (root/'calls').open('a') as f:f.write(a.action+'\n')
        configured=root/'configured';running=root/'running';ready=root/'ready'
        if a.action=='configure':configured.write_text('yes')
        if a.action=='serve':
            running.write_text(str(os.getpid()))
            def stop(*_):
                running.unlink(missing_ok=True);sys.exit(0)
            signal.signal(signal.SIGTERM,stop)
            while True:time.sleep(.05)
        if a.action=='stop' and running.exists():
            os.kill(int(running.read_text()),signal.SIGTERM);time.sleep(.1)
        alive=False
        if running.exists():
            try:os.kill(int(running.read_text()),0);alive=True
            except (ProcessLookupError,ValueError):pass
        print(json.dumps(dict(configured=configured.exists(),running=alive,pid=int(running.read_text()) if alive else None,subscribed=ready.exists(),identity=dict(connected=ready.exists(),active_subscriptions=1 if ready.exists() else 0))))
        """#
        try script.write(to: executable, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: executable.path)
        let defaults = UserDefaults(suiteName: "dbond-test-" + root.lastPathComponent)!
        let connector = DotConnector(executable: executable, stateDirectory: root, routeURL: root.appendingPathComponent("route.json"), defaults: defaults)
        addTeardownBlock { defaults.removePersistentDomain(forName: "dbond-test-" + root.lastPathComponent) }
        return (root, connector, defaults)
    }

    private func result(_ action: (@escaping (Result<DotConnectorStatus, Error>) -> Void) -> Void) throws -> DotConnectorStatus {
        let done = expectation(description: "connector action")
        var value: Result<DotConnectorStatus, Error>?
        action { value = $0; done.fulfill() }
        wait(for: [done], timeout: 15)
        return try XCTUnwrap(value).get()
    }

    func testDisabledOrUnconfiguredStartupNeverSpawnsHost() throws {
        let (root, connector, defaults) = try fixture()
        defer { connector.shutdown(); try? FileManager.default.removeItem(at: root); defaults.removePersistentDomain(forName: "dbond-test-" + root.lastPathComponent) }
        connector.startIfEnabled { _ in XCTFail("Disabled host must not run a command") }
        XCTAssertFalse(FileManager.default.fileExists(atPath: root.appendingPathComponent("calls").path))
        XCTAssertFalse(try result(connector.start).configured)
        XCTAssertFalse(FileManager.default.fileExists(atPath: root.appendingPathComponent("running").path))
        XCTAssertEqual(connector.selectedRoute, "legacy")
    }

    func testOwnedHostStartsOnceAndStopsWithoutChangingLegacy() throws {
        let (root, connector, defaults) = try fixture()
        defer { connector.shutdown(); try? FileManager.default.removeItem(at: root) }
        XCTAssertTrue(try result { connector.configure(publicURL: "https://example.com", redirectURI: "https://chatgpt.com/callback", completion: $0) }.running)
        XCTAssertTrue(defaults.bool(forKey: DotConnector.enabledPreference))
        XCTAssertEqual(connector.selectedRoute, "legacy")
        XCTAssertTrue(try result(connector.start).running)
        let calls = try String(contentsOf: root.appendingPathComponent("calls"), encoding: .utf8).split(separator: "\n")
        XCTAssertEqual(calls.filter { $0 == "serve" }.count, 1)
        XCTAssertFalse(try result(connector.stop).running)
        XCTAssertFalse(defaults.bool(forKey: DotConnector.enabledPreference))
        XCTAssertFalse(FileManager.default.fileExists(atPath: root.appendingPathComponent("route.json").path))
    }

    func testRestartedAppReusesAndStopsVerifiedExistingHost() throws {
        let (root, connector, defaults) = try fixture()
        let restarted = DotConnector(executable: root.appendingPathComponent("connector"), stateDirectory: root,
                                     routeURL: root.appendingPathComponent("route.json"), defaults: defaults)
        defer { restarted.shutdown(); connector.shutdown(); try? FileManager.default.removeItem(at: root) }
        _ = try result { connector.configure(publicURL: "https://example.com", redirectURI: "https://chatgpt.com/callback", completion: $0) }
        XCTAssertTrue(try result(restarted.start).running)
        let calls = try String(contentsOf: root.appendingPathComponent("calls"), encoding: .utf8).split(separator: "\n")
        XCTAssertEqual(calls.filter { $0 == "serve" }.count, 1)
        XCTAssertFalse(try result(restarted.stop).running)
    }

    func testLegacyRevertWorksWhenOwnedRuntimeIsMissing() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let connector = DotConnector(executable: nil, stateDirectory: root, routeURL: root.appendingPathComponent("route.json"))
        _ = try result { connector.selectRoute("legacy", completion: $0) }
        XCTAssertEqual(connector.selectedRoute, "legacy")
    }

    func testExplicitQuitStopsAdoptedOwnedHostWithoutRestart() throws {
        let (root, connector, defaults) = try fixture()
        let external = Process()
        defer {
            if external.isRunning { external.terminate() }
            connector.shutdown()
            try? FileManager.default.removeItem(at: root)
        }
        try Data().write(to: root.appendingPathComponent("configured"))
        external.executableURL = root.appendingPathComponent("connector")
        external.arguments = ["--state-dir", root.path, "serve"]
        external.standardOutput = FileHandle.nullDevice
        external.standardError = FileHandle.nullDevice
        try external.run()
        let ready = Date().addingTimeInterval(3)
        while !FileManager.default.fileExists(atPath: root.appendingPathComponent("running").path) && Date() < ready {
            RunLoop.main.run(until: Date().addingTimeInterval(0.02))
        }
        XCTAssertTrue(try result(connector.start).running)
        connector.shutdown()
        let settle = Date().addingTimeInterval(2.5)
        while Date() < settle { RunLoop.main.run(until: Date().addingTimeInterval(0.02)) }
        XCTAssertFalse(external.isRunning)
        XCTAssertTrue(defaults.bool(forKey: DotConnector.enabledPreference), "Quit preserves next-launch choice")
        let calls = try String(contentsOf: root.appendingPathComponent("calls"), encoding: .utf8).split(separator: "\n")
        XCTAssertEqual(calls.filter { $0 == "serve" }.count, 1)
        XCTAssertTrue(calls.contains("stop"))
        XCTAssertFalse(FileManager.default.fileExists(atPath: root.appendingPathComponent("route.json").path))
    }

    func testAdoptedHostExitRecoversWithoutPollingOrDuplicateStart() throws {
        let (root, connector, _) = try fixture()
        let external = Process()
        defer {
            if external.isRunning { external.terminate() }
            connector.shutdown()
            try? FileManager.default.removeItem(at: root)
        }
        try Data().write(to: root.appendingPathComponent("configured"))
        external.executableURL = root.appendingPathComponent("connector")
        external.arguments = ["--state-dir", root.path, "serve"]
        external.standardOutput = FileHandle.nullDevice
        external.standardError = FileHandle.nullDevice
        try external.run()
        let deadline = Date().addingTimeInterval(3)
        while !FileManager.default.fileExists(atPath: root.appendingPathComponent("running").path) && Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.02))
        }
        XCTAssertTrue(try result(connector.start).running)
        let recovered = expectation(description: "adopted connector restarted")
        connector.onHostRecovered = { recovered.fulfill() }
        XCTAssertEqual(kill(external.processIdentifier, SIGKILL), 0)
        external.waitUntilExit()
        wait(for: [recovered], timeout: 12)
        let calls = try String(contentsOf: root.appendingPathComponent("calls"), encoding: .utf8).split(separator: "\n")
        XCTAssertEqual(calls.filter { $0 == "serve" }.count, 2)
        _ = try result(connector.stop)
        XCTAssertEqual(connector.selectedRoute, "legacy")
    }

    func testKilledOwnedHostRecoversWithoutDuplicateOrLegacyMutation() throws {
        let (root, connector, _) = try fixture()
        defer { connector.shutdown(); try? FileManager.default.removeItem(at: root) }
        _ = try result { connector.configure(publicURL: "https://example.com", redirectURI: "https://chatgpt.com/callback", completion: $0) }
        let recovered = expectation(description: "owned connector restarted")
        connector.onHostRecovered = { recovered.fulfill() }
        let firstPID = try XCTUnwrap(Int32(String(contentsOf: root.appendingPathComponent("running"), encoding: .utf8)))
        XCTAssertEqual(kill(firstPID, SIGKILL), 0)
        wait(for: [recovered], timeout: 12)
        let secondPID = try XCTUnwrap(Int32(String(contentsOf: root.appendingPathComponent("running"), encoding: .utf8)))
        XCTAssertNotEqual(firstPID, secondPID)
        let calls = try String(contentsOf: root.appendingPathComponent("calls"), encoding: .utf8).split(separator: "\n")
        XCTAssertEqual(calls.filter { $0 == "serve" }.count, 2)
        XCTAssertEqual(connector.selectedRoute, "legacy")
        _ = try result(connector.stop)
    }

    func testRouteSelectionRequiresVerifiedIdentityAndCanRevert() throws {
        let (root, connector, _) = try fixture()
        defer { connector.shutdown(); try? FileManager.default.removeItem(at: root) }
        _ = try result { connector.configure(publicURL: "https://example.com", redirectURI: "https://chatgpt.com/callback", completion: $0) }
        XCTAssertThrowsError(try result { connector.selectRoute("owned", completion: $0) })
        XCTAssertEqual(connector.selectedRoute, "legacy")
        try Data().write(to: root.appendingPathComponent("ready"))
        _ = try result { connector.selectRoute("owned", completion: $0) }
        XCTAssertEqual(connector.selectedRoute, "owned")
        let mode = try FileManager.default.attributesOfItem(atPath: root.appendingPathComponent("route.json").path)[.posixPermissions] as? NSNumber
        XCTAssertEqual(mode?.intValue, 0o600)
        _ = try result { connector.selectRoute("legacy", completion: $0) }
        XCTAssertEqual(connector.selectedRoute, "legacy")
    }
}
