import AppKit
import Foundation
import BodhEngine

/// Install command line tool: `/usr/local/bin/bodh` as a symlink to the bundled launcher, with
/// the system's administrator prompt; nothing else is set. BODH_HOME is shown for the shell.
enum CLIInstaller {
    static let link = "/usr/local/bin/bodh"

    static var isInstalled: Bool {
        guard let target = Paths.engineCLI else { return false }
        return (try? FileManager.default.destinationOfSymbolicLink(atPath: link)) == target.path
    }

    static func install() -> String {
        guard let target = Paths.engineCLI else { return "The engine is not in the app bundle." }
        let script = "mkdir -p /usr/local/bin && ln -sf '\(target.path.replacingOccurrences(of: "'", with: "'\\''"))' '\(link)'"
        return runPrivileged(script) ?? "Installed: bodh on the command line. Add to your shell profile: export BODH_HOME=\"\(Paths.home.path)\""
    }

    static func remove() -> String {
        runPrivileged("rm -f '\(link)'") ?? "Removed \(link)."
    }

    /// AppleScript's administrator prompt, the plain way to do one privileged file operation.
    private static func runPrivileged(_ shell: String) -> String? {
        let escaped = shell.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"")
        let source = "do shell script \"\(escaped)\" with administrator privileges"
        var error: NSDictionary?
        guard let script = NSAppleScript(source: source) else { return "The command could not be prepared." }
        script.executeAndReturnError(&error)
        if let error = error {
            let message = error[NSAppleScript.errorMessage] as? String ?? "cancelled"
            return "Not done: \(message)"
        }
        return nil
    }
}

/// Export diagnostics: a zip with the app's log, the engine's log tail, `/api/health`, the
/// settings (never a key: the engine reads them from the keychain and its answers carry none),
/// written where the user chooses. Nothing is sent anywhere.
@MainActor
enum Diagnostics {
    static func export(model: AppModel) async -> URL? {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "bodh-diagnostics-\(Int(Date().timeIntervalSince1970)).zip"
        panel.canCreateDirectories = true
        guard panel.runModal() == .OK, let destination = panel.url else { return nil }
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("bodh-diagnostics-\(UUID().uuidString)", isDirectory: true)
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        var report: [String] = ["bodh for Mac diagnostics", "app: \(Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "dev")", "engine state: \(model.engineState)"]
        if let client = model.client {
            if let health = try? await client.health() {
                report.append("engine: \(health.version), schema \(health.schemaVersion), mode \(health.mode), setup needed \(health.setupNeeded)")
                report.append("data: \(health.dataDir)")
            }
            if let lines = try? await client.log(lines: 400) {
                try? lines.joined(separator: "\n").write(to: folder.appendingPathComponent("engine.log"), atomically: true, encoding: .utf8)
            }
            if let settings = try? await client.settings() {
                let text = settings.settings.keys.sorted().map { "\($0) = \(settings.settings[$0]!.description) (\(settings.sources[$0] ?? ""))" }.joined(separator: "\n")
                try? text.write(to: folder.appendingPathComponent("settings.txt"), atomically: true, encoding: .utf8)
            }
        }
        try? report.joined(separator: "\n").write(to: folder.appendingPathComponent("report.txt"), atomically: true, encoding: .utf8)
        try? AppLog.tail(400).joined(separator: "\n").write(to: folder.appendingPathComponent("app.log"), atomically: true, encoding: .utf8)
        let zip = Process()
        zip.executableURL = URL(fileURLWithPath: "/usr/bin/zip")
        zip.currentDirectoryURL = folder
        zip.arguments = ["-q", "-r", destination.path, "."]
        try? zip.run()
        zip.waitUntilExit()
        try? FileManager.default.removeItem(at: folder)
        return zip.terminationStatus == 0 ? destination : nil
    }
}

/// Where things are, for Reveal in Finder and the About box.
@MainActor
enum Reveal {
    static func dataFolder() {
        NSWorkspace.shared.activateFileViewerSelecting([Paths.home])
    }

    static func configFile() {
        NSWorkspace.shared.open(Paths.configFile)
    }
}
