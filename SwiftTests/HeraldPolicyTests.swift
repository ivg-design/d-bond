import XCTest
@testable import DBondCore

final class HeraldPolicyTests: XCTestCase {
    func testOnlyConfirmedHeraldOptionCreatesTheSharedReceiverRule() throws {
        XCTAssertNil(OwnerSOP.pairedHeraldNotifications(destination: "dot", enabled: true))
        XCTAssertNil(OwnerSOP.pairedHeraldNotifications(destination: "herald", enabled: false))
        let sop = try XCTUnwrap(OwnerSOP.pairedHeraldNotifications(destination: "herald", enabled: true))
        XCTAssertEqual(sop.version, 1)
        XCTAssertEqual(sop.rules.map(\.id), ["general-notifications"])
        XCTAssertTrue(sop.rules[0].text.contains("plain Herald notification"))
        XCTAssertTrue(sop.rules[0].text.contains("active service paired on this Mac"))
    }
}
