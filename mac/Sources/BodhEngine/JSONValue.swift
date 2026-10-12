import Foundation

/// Any JSON value, for the parts of the engine's answers whose shape the schema decides at
/// run time (a setting's value, a folder policy, a tick). Encodes integral numbers as integers,
/// so a cut-off of 35 reaches config.yaml as `35`, not `35.0`.
public enum JSONValue: Equatable, Hashable, Codable, CustomStringConvertible {
    case string(String)
    case number(Double)
    case bool(Bool)
    case null
    case array([JSONValue])
    case object([String: JSONValue])

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let b = try? container.decode(Bool.self) {
            self = .bool(b)
        } else if let n = try? container.decode(Double.self) {
            self = .number(n)
        } else if let s = try? container.decode(String.self) {
            self = .string(s)
        } else if let a = try? container.decode([JSONValue].self) {
            self = .array(a)
        } else if let o = try? container.decode([String: JSONValue].self) {
            self = .object(o)
        } else {
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "not a JSON value")
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let s): try container.encode(s)
        case .number(let n):
            if n == n.rounded() && abs(n) < 1e15 {
                try container.encode(Int64(n))
            } else {
                try container.encode(n)
            }
        case .bool(let b): try container.encode(b)
        case .null: try container.encodeNil()
        case .array(let a): try container.encode(a)
        case .object(let o): try container.encode(o)
        }
    }

    public var description: String {
        switch self {
        case .string(let s): return s
        case .number(let n): return n == n.rounded() && abs(n) < 1e15 ? String(Int64(n)) : String(n)
        case .bool(let b): return b ? "true" : "false"
        case .null: return ""
        case .array(let a): return "[" + a.map { $0.description }.joined(separator: ", ") + "]"
        case .object(let o): return "{" + o.keys.sorted().map { "\($0): \(o[$0]!.description)" }.joined(separator: ", ") + "}"
        }
    }

    public var stringValue: String? { if case .string(let s) = self { return s } else { return nil } }
    public var doubleValue: Double? { if case .number(let n) = self { return n } else { return nil } }
    public var intValue: Int? { if case .number(let n) = self { return Int(n) } else { return nil } }
    public var boolValue: Bool? { if case .bool(let b) = self { return b } else { return nil } }
    public var isNull: Bool { if case .null = self { return true } else { return false } }
    public var arrayValue: [JSONValue]? { if case .array(let a) = self { return a } else { return nil } }
    public var objectValue: [String: JSONValue]? { if case .object(let o) = self { return o } else { return nil } }

    public subscript(key: String) -> JSONValue? { objectValue?[key] }
}
