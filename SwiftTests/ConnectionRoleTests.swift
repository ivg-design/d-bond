import XCTest
@testable import DBondCore

final class ConnectionRoleTests: XCTestCase {
    func testLegacyRoutesAndUnknownRolesUseStableGrouping() throws {
        func load(_ name: String, destination: String = "dot", role: String? = nil) throws -> Service {
            var row = ["id": "test", "digest": "test", "name": name, "destination": destination]
            row["role"] = role
            return try JSONDecoder().decode(Service.self, from: JSONEncoder().encode(row))
        }
        XCTAssertEqual(try load("Codex / Design").connectionRole, .codexSession)
        XCTAssertEqual(try load("Claude").connectionRole, .claudeSession)
        XCTAssertEqual(try load("Claude / Design", destination: "peer").connectionRole, .claudeSession)
        XCTAssertEqual(try load("Dot / Dispatch").connectionRole, .dotSession)
        XCTAssertEqual(try load("Watcher / Dot").connectionRole, .workflow)
        XCTAssertEqual(try load("Claude / Notifications", destination: "herald").connectionRole, .workflow)
        XCTAssertEqual(try load("Dot / Reports", role: "workflow").connectionRole, .workflow)
        XCTAssertEqual(try load("Renamed", role: "codexSession").connectionRole, .codexSession)
        XCTAssertEqual(try load("Codex / Unknown", role: "futureRole").connectionRole, .workflow)
    }
}
