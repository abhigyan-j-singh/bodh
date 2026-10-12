import Foundation
import os

/// The app's own log: `os.Logger` (Console.app, with privacy) and a short file beside the
/// project for Export diagnostics. Nothing here ever carries a key or a password: the engine
/// redacts its own lines, and the app logs states and counts, not values.
enum AppLog {
    static let logger = Logger(subsystem: "in.antriksh.bodh", category: "app")
    private static let queue = DispatchQueue(label: "in.antriksh.bodh.log")
    private static let formatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return f
    }()

    static func info(_ message: String) {
        logger.info("\(message, privacy: .public)")
        append("INFO  " + message)
    }

    static func warning(_ message: String) {
        logger.warning("\(message, privacy: .public)")
        append("WARN  " + message)
    }

    static func error(_ message: String) {
        logger.error("\(message, privacy: .public)")
        append("ERROR " + message)
    }

    /// A line the engine printed on stderr: kept in the file, never in the system log with
    /// details (an article title could be in it).
    static func engine(_ line: String) {
        logger.debug("engine: \(line, privacy: .private)")
        append("ENGINE " + line)
    }

    private static func append(_ line: String) {
        queue.async {
            let url = Paths.appLog
            let text = formatter.string(from: Date()) + " " + line + "\n"
            guard let data = text.data(using: .utf8) else { return }
            if let handle = try? FileHandle(forWritingTo: url) {
                defer { try? handle.close() }
                _ = try? handle.seekToEnd()
                try? handle.write(contentsOf: data)
            } else {
                try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
                try? data.write(to: url)
            }
            trim(url)
        }
    }

    /// Keep the file under about a megabyte: drop the first half when it grows past it.
    private static func trim(_ url: URL) {
        guard let size = (try? FileManager.default.attributesOfItem(atPath: url.path)[.size] as? Int), size > 1_000_000,
              let text = try? String(contentsOf: url, encoding: .utf8) else { return }
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false)
        let kept = lines.suffix(lines.count / 2).joined(separator: "\n")
        try? kept.write(to: url, atomically: true, encoding: .utf8)
    }

    static func tail(_ lines: Int = 200) -> [String] {
        guard let text = try? String(contentsOf: Paths.appLog, encoding: .utf8) else { return [] }
        return text.split(separator: "\n").suffix(lines).map(String.init)
    }
}
