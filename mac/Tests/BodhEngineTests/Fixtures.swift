import Foundation
import XCTest
@testable import BodhEngine

/// The fixtures the engine's own suite writes (tests/fixtures/api, one truth): read from the
/// checkout, so the Swift models decode what the engine really sends.
enum Fixtures {
    static var directory: URL {
        // mac/Tests/BodhEngineTests/Fixtures.swift -> <checkout>/tests/fixtures/api
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("tests/fixtures/api")
    }

    static func data(_ name: String) throws -> Data {
        try Data(contentsOf: directory.appendingPathComponent(name))
    }

    static func decode<T: Decodable>(_ type: T.Type, _ name: String) throws -> T {
        try EngineClient.decoder.decode(type, from: data(name))
    }
}
