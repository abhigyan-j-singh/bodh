import Foundation
import BodhEngine

/// The owner's CLI install, found on the first launch: `~/bodh` (or BODH_HOME) with a
/// config.yaml while Application Support holds no project yet. The offer: move the folder,
/// import .env into the keychain, remove the launchd jobs, start at login. Each line a
/// checkbox, all on; `Undo migration` in Settings > Advanced moves it back.
struct MigrationOffer: Equatable {
    var source: URL
    var hasEnv: Bool
    var hasLaunchdJobs: Bool

    static func detect() -> MigrationOffer? {
        guard Paths.home == Paths.applicationSupport,
              !FileManager.default.fileExists(atPath: Paths.configFile.path) else { return nil }
        let home = FileManager.default.homeDirectoryForCurrentUser
        var candidates = [Paths.legacyHome, home.appendingPathComponent("triage", isDirectory: true)]  // the old name's home
        if let env = ProcessInfo.processInfo.environment["TRIAGE_HOME"], !env.isEmpty {
            candidates.append(URL(fileURLWithPath: (env as NSString).expandingTildeInPath, isDirectory: true))
        }
        for source in candidates where FileManager.default.fileExists(atPath: source.appendingPathComponent("config.yaml").path) {
            let agents = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/LaunchAgents")
            let labels = ["bodh.run", "bodh.weekly", "bodh.report", "bodh.console", "com.antriksh.triage"]
            let jobs = labels.contains { FileManager.default.fileExists(atPath: agents.appendingPathComponent($0 + ".plist").path) }
            return MigrationOffer(
                source: source,
                hasEnv: FileManager.default.fileExists(atPath: source.appendingPathComponent(".env").path),
                hasLaunchdJobs: jobs
            )
        }
        return nil
    }
}

enum Migration {
    struct Choices {
        var move = true
        var importEnv = true
        var removeJobs = true
        var loginItem = true
    }

    /// Runs the steps that need the command line (before the engine starts): the move and the
    /// jobs. The .env import happens through the service once the engine is up (`finish`).
    static func run(_ offer: MigrationOffer, choices: Choices) -> [String] {
        var lines: [String] = []
        if choices.removeJobs && offer.hasLaunchdJobs {
            let result = EngineCLI.run(["schedule", "uninstall"], home: offer.source)
            lines.append("launchd jobs: " + (result.code == 0 ? "removed" : "could not be removed (\(result.output))"))
        }
        if choices.move {
            let result = EngineCLI.run(["migrate-home", offer.source.path, Paths.applicationSupport.path], home: offer.source)
            lines.append(result.code == 0 ? result.output : "the move failed: \(result.output)")
            UserDefaults.standard.set(offer.source.path, forKey: "migration.source")
        }
        return lines
    }

    /// The .env import, once the engine answers.
    static func finish(client: EngineClient, choices: Choices) async -> String? {
        guard choices.importEnv else { return nil }
        do {
            let keys = try await client.importEnv(delete: true)
            let imported = keys.keys.filter { $0.set }.map { $0.provider }
            return "keys moved to the Keychain: " + (imported.isEmpty ? "none found" : imported.joined(separator: ", "))
        } catch {
            return "the .env import failed: \(error)"
        }
    }

    static var undoSource: URL? {
        UserDefaults.standard.string(forKey: "migration.source").map { URL(fileURLWithPath: $0, isDirectory: true) }
    }

    /// Back to the folder it came from; the keys stay in the keychain (the CLI reads them there).
    static func undo() -> String {
        guard let source = undoSource else { return "Nothing to undo." }
        let result = EngineCLI.run(["migrate-home", Paths.applicationSupport.path, source.path], home: Paths.applicationSupport)
        if result.code == 0 { UserDefaults.standard.removeObject(forKey: "migration.source") }
        return result.output + "\nTo bring the launchd jobs back: cd \(source.path) && bodh schedule install"
    }
}
