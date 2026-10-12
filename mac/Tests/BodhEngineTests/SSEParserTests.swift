import XCTest
@testable import BodhEngine

final class SSEParserTests: XCTestCase {
    func testParsesTheEngineFixtureFrames() throws {
        let text = String(decoding: try Fixtures.data("events.txt"), as: UTF8.self)
        var parser = SSEParser()
        let events = parser.feed(text)
        XCTAssertFalse(events.isEmpty)
        XCTAssertEqual(events.first?.name, "tick")
        XCTAssertNotNil(events.first?.data["state"])
        if let named = events.first(where: { $0.name != "tick" }) {
            XCTAssertNotNil(named.id)
            XCTAssertEqual(parser.lastEventID, named.id)
        }
    }

    func testLinesSplitAcrossChunksAndCRLF() {
        var parser = SSEParser()
        XCTAssertEqual(parser.feed("event: ti"), [])
        XCTAssertEqual(parser.feed("ck\r\ndata: {\"a\": 1}\r\n"), [])
        let events = parser.feed("\r\n: a comment\n\nid: 7\nevent: run\ndata: {\"state\": \"started\"}\n\n")
        XCTAssertEqual(events.count, 2)
        XCTAssertEqual(events[0].name, "tick")
        XCTAssertEqual(events[0].data["a"], .number(1))
        XCTAssertNil(events[0].id)
        XCTAssertEqual(events[1].id, 7)
        XCTAssertEqual(events[1].name, "run")
        XCTAssertEqual(events[1].data["state"], .string("started"))
        XCTAssertEqual(parser.lastEventID, 7)
    }

    func testMultilineDataAndNonJSON() {
        var parser = SSEParser()
        let events = parser.feed("data: one\ndata: two\n\n")
        XCTAssertEqual(events, [EngineEvent(id: nil, name: "message", data: .string("one\ntwo"))])
    }
}
