import Foundation
import XCTest
@testable import DBondCore

final class OpenAITunnelClientInstallerTests: XCTestCase {
    private let brew = URL(fileURLWithPath: "/test/Homebrew/bin/brew")
    private let client = URL(fileURLWithPath: "/test/Homebrew/bin/tunnel-client")

    func testExistingClientRequiresNoHomebrewOrMutation() throws {
        let existing = try OpenAITunnelClientInstaller.installIfNeeded(findClient: { self.client }, brew: nil) { _, _, _ in
            XCTFail("Existing client must be preserved"); return false
        }
        XCTAssertEqual(existing, client)
    }

    func testInstallationUsesTheOfficialFormulaAndChecksItWithoutStartingAService() throws {
        var found: URL?, commands: [[String]] = []
        let installed = try OpenAITunnelClientInstaller.installIfNeeded(findClient: { found }, brew: brew) { executable, args, _ in
            commands.append([executable.path] + args)
            if args.first == "install" { found = self.client }
            return true
        }
        XCTAssertEqual(installed, client)
        XCTAssertEqual(commands, [[brew.path, "tap", "openai/tools", "https://github.com/openai/homebrew-tools"],
                                  [brew.path, "install", "--formula", "openai/tools/tunnel-client"], [client.path, "--version"]])
    }

    func testConflictingTapStopsBeforeInstall() {
        var calls = 0
        XCTAssertThrowsError(try OpenAITunnelClientInstaller.installIfNeeded(findClient: { nil }, brew: brew) { _, args, _ in
            calls += 1
            XCTAssertEqual(args.first, "tap")
            return false
        }) { error in
            guard case OpenAITunnelClientInstaller.Failure.tap = error else { return XCTFail("Unexpected error: \(error)") }
        }
        XCTAssertEqual(calls, 1)
    }

    func testClientAppearingDuringTapSetupIsPreserved() throws {
        var found: URL?
        let result = try OpenAITunnelClientInstaller.installIfNeeded(findClient: { found }, brew: brew) { _, args, _ in
            XCTAssertEqual(args.first, "tap"); found = self.client; return true
        }
        XCTAssertEqual(result, client)
    }

    func testMissingHomebrewAndUnusableClientHaveSeparateErrors() {
        XCTAssertThrowsError(try OpenAITunnelClientInstaller.installIfNeeded(findClient: { nil }, brew: nil) { _, _, _ in false }) { error in
            guard case OpenAITunnelClientInstaller.Failure.missingHomebrew = error else { return XCTFail("Unexpected error") }
        }
        XCTAssertThrowsError(try OpenAITunnelClientInstaller.installIfNeeded(findClient: { nil }, brew: brew) { _, _, _ in true }) { error in
            guard case OpenAITunnelClientInstaller.Failure.verification = error else { return XCTFail("Unexpected error") }
        }
    }

    func testDiscoveryAllowsExecutableFilesAndLinksInPriorityOrder() throws {
        let manager = FileManager.default
        let root = manager.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? manager.removeItem(at: root) }
        let one = root.appendingPathComponent("one"), two = root.appendingPathComponent("two")
        for dir in [one, two] { try manager.createDirectory(at: dir, withIntermediateDirectories: true) }
        let custom = one.appendingPathComponent("tunnel-client"), owned = two.appendingPathComponent("tunnel-client")
        try manager.createDirectory(at: custom, withIntermediateDirectories: true)
        try Data().write(to: owned)
        try manager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: owned.path)
        XCTAssertEqual(OpenAITunnelClientInstaller.executable("tunnel-client", in: [one, two])?.path, owned.path)
        try manager.removeItem(at: custom)
        try manager.createSymbolicLink(at: custom, withDestinationURL: owned)
        XCTAssertEqual(OpenAITunnelClientInstaller.executable("tunnel-client", in: [one, two])?.path, custom.path)
    }

    func testHomebrewClientIsPinnedToItsKegAcrossRelinking() throws {
        let manager = FileManager.default
        let root = manager.temporaryDirectory.resolvingSymlinksInPath().appendingPathComponent("D-Bond QA \(UUID())")
        defer { try? manager.removeItem(at: root) }
        let bin = root.appendingPathComponent("bin")
        let link = bin.appendingPathComponent("tunnel-client")
        try manager.createDirectory(at: bin, withIntermediateDirectories: true)
        var pinned: [URL] = []
        for version in ["0.0.9", "0.0.20"] {
            let keg = root.appendingPathComponent("Cellar/tunnel-client/\(version)")
            let native = keg.appendingPathComponent("libexec/tunnel-client")
            let wrapper = keg.appendingPathComponent("bin/tunnel-client")
            for file in [native, wrapper] {
                try manager.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
            }
            try manager.copyItem(at: URL(fileURLWithPath: "/bin/echo"), to: native)
            let script = "#!/bin/bash\nexec \"\(native.path)\" \"$@\"\n"
            try Data(script.utf8).write(to: wrapper)
            try manager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: wrapper.path)
            if !pinned.isEmpty { try manager.removeItem(at: link) }
            try manager.createSymbolicLink(at: link, withDestinationURL: wrapper)
            let found = try XCTUnwrap(OpenAITunnelClientInstaller.client(in: [bin]))
            XCTAssertEqual(found.path, native.path)
            pinned.append(found)
        }
        XCTAssertNotEqual(pinned.first, pinned.last)
        XCTAssertTrue(manager.isExecutableFile(atPath: pinned[0].path))

        // Unknown custom wrappers must retain their own launcher behavior.
        try Data("#!/bin/bash\nexec \"/bin/echo\" \"$@\"\n".utf8).write(to: link.resolvingSymlinksInPath())
        XCTAssertEqual(OpenAITunnelClientInstaller.client(in: [bin])?.path, link.path)
    }
}
