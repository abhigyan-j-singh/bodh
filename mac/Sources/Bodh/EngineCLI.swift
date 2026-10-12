import Foundation
import BodhEngine

/// The bundled command line, run and waited for: `bodh init --skeleton` before the first
/// start, `bodh migrate-home` and `bodh schedule uninstall` for the migration, `bodh version`
/// for the About box. The engine's own service is for everything that happens while it runs.
enum EngineCLI {
    struct Result {
        var code: Int32
        var output: String
    }

    static func run(_ arguments: [String], home: URL? = nil, timeout: TimeInterval = 120) -> Result {
        guard let python = Paths.enginePython else {
            return Result(code: 127, output: "The engine is not in the app bundle.")
        }
        let process = Process()
        process.executableURL = python
        process.arguments = ["-m", "bodh.cli"] + arguments
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        environment["BODH_HOME"] = (home ?? Paths.home).path
        process.environment = environment
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        do {
            try process.run()
        } catch {
            return Result(code: 126, output: error.localizedDescription)
        }
        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.05)
        }
        if process.isRunning { process.terminate() }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return Result(code: process.terminationStatus, output: String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines))
    }

    /// The project files when there are none yet (the engine refuses to start without config.yaml).
    static func ensureProject(at home: URL) -> Result? {
        if FileManager.default.fileExists(atPath: home.appendingPathComponent("config.yaml").path) {
            return nil
        }
        let result = run(["init", home.path, "--skeleton"], home: home)
        AppLog.info("init --skeleton exited \(result.code)")
        return result
    }

    static func version() -> String {
        let result = run(["version"])
        return result.code == 0 ? result.output : "unknown"
    }
}
