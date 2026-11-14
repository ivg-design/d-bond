import Foundation
import XCTest
@testable import DBondCore

final class EventMessagePreviewTests: XCTestCase {
    private func preview(_ object: [String: Any]) throws -> String? {
        let data = try JSONSerialization.data(withJSONObject: object)
        return EventMessagePreview.make(data, eventID: "fixture-event", service: "fixture-service")
    }

    func testOnlyExplicitDisplayFieldsAreUsedForAnyNamedService() throws {
        XCTAssertEqual(try preview(["title": "Example item saved", "record": ["content": "Opaque record text"]]), "Example item saved")
        XCTAssertEqual(try preview(["text": "Needs review. Provide more context.", "details": ["state": "pending"]]), "Needs review. Provide more context.")
        XCTAssertNil(try preview(["protocol": "custom-result/1", "opaque": ["content": "Hidden application data"]]))
        XCTAssertNil(try preview(["metadata": ["secret": "Hidden transport value"]]))
        XCTAssertNil(try preview(["kind": "service.policy", "text": "Hidden owner rule"]))
    }
}
