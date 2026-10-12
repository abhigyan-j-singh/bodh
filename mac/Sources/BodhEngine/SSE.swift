import Foundation

/// A server-sent events parser: feed it the bytes as they arrive, take the frames it has
/// finished. Lines may be split across chunks; `\r\n` and `\n` both end a line; `data:` lines
/// join with newlines; a comment line (`:`) is ignored; a blank line dispatches.
public struct SSEParser {
    private var buffer = ""
    private var name = "message"
    private var dataLines: [String] = []
    private var id: Int?
    public private(set) var lastEventID: Int?

    public init() {}

    public mutating func feed(_ text: String) -> [EngineEvent] {
        buffer += text
        var out: [EngineEvent] = []
        while let range = buffer.range(of: "\n") {
            var line = String(buffer[buffer.startIndex..<range.lowerBound])
            buffer.removeSubrange(buffer.startIndex..<range.upperBound)
            if line.hasSuffix("\r") { line.removeLast() }
            if let event = take(line: line) { out.append(event) }
        }
        return out
    }

    public mutating func feed(_ data: Data) -> [EngineEvent] {
        feed(String(decoding: data, as: UTF8.self))
    }

    private mutating func take(line: String) -> EngineEvent? {
        if line.isEmpty {
            guard !dataLines.isEmpty else { name = "message"; id = nil; return nil }
            let text = dataLines.joined(separator: "\n")
            let event = EngineEvent(id: id, name: name, data: SSEParser.decode(text))
            if let id = id { lastEventID = id }
            name = "message"
            dataLines = []
            id = nil
            return event
        }
        if line.hasPrefix(":") { return nil }
        let field: String
        let value: String
        if let colon = line.firstIndex(of: ":") {
            field = String(line[line.startIndex..<colon])
            var rest = String(line[line.index(after: colon)...])
            if rest.hasPrefix(" ") { rest.removeFirst() }
            value = rest
        } else {
            field = line
            value = ""
        }
        switch field {
        case "event": name = value
        case "data": dataLines.append(value)
        case "id": id = Int(value)
        default: break  // retry and unknown fields: nothing to do
        }
        return nil
    }

    static func decode(_ text: String) -> JSONValue {
        guard let data = text.data(using: .utf8), let value = try? JSONDecoder().decode(JSONValue.self, from: data) else {
            return .string(text)
        }
        return value
    }
}
