import Foundation

/// Where everything is: the project (config.yaml, data/, profile/) under Application Support,
/// the token file beside it, the engine inside the bundle (or, for `swift run` during
/// development, wherever BODH_ENGINE points), the app's own log.
enum Paths {
    static let appName = "bodh"

    static var applicationSupport: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        return base.appendingPathComponent(appName, isDirectory: true)
    }

    /// The project folder the engine runs on: `BODH_HOME` when the environment sets it (a
    /// developer's checkout), else Application Support/bodh.
    static var home: URL {
        if let env = ProcessInfo.processInfo.environment["BODH_HOME"], !env.isEmpty {
            return URL(fileURLWithPath: (env as NSString).expandingTildeInPath, isDirectory: true)
        }
        return applicationSupport
    }

    static var tokenFile: URL { applicationSupport.appendingPathComponent("token") }
    static var appLog: URL { applicationSupport.appendingPathComponent("app.log") }
    static var configFile: URL { home.appendingPathComponent("config.yaml") }
    static var dataDirectory: URL { home.appendingPathComponent("data", isDirectory: true) }

    /// The CLI's old home, offered for migration on the first launch.
    static var legacyHome: URL { FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("bodh", isDirectory: true) }

    /// The bundled engine: `Contents/Frameworks/engine/python/bin/python3` in the app bundle,
    /// `BODH_ENGINE` (a folder made by scripts/build_engine.sh) when developing.
    static var engineDirectory: URL? {
        if let env = ProcessInfo.processInfo.environment["BODH_ENGINE"], !env.isEmpty {
            return URL(fileURLWithPath: env, isDirectory: true)
        }
        let bundled = Bundle.main.bundleURL
            .appendingPathComponent("Contents/Frameworks/engine", isDirectory: true)
        if FileManager.default.fileExists(atPath: bundled.appendingPathComponent("python/bin/python3").path) {
            return bundled
        }
        return nil
    }

    static var enginePython: URL? { engineDirectory?.appendingPathComponent("python/bin/python3") }
    static var engineCLI: URL? { engineDirectory?.appendingPathComponent("bodh") }

    static func ensureDirectories() throws {
        try FileManager.default.createDirectory(at: applicationSupport, withIntermediateDirectories: true)
    }
}
