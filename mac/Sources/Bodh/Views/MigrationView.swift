import SwiftUI
import BodhEngine

/// The first launch on a Mac that already runs bodh from the terminal: move the folder into
/// Application Support, import .env into the Keychain, remove the launchd jobs, start at
/// login. Each line a checkbox, all on; one Continue; the summary names every path touched.
struct MigrationView: View {
    @EnvironmentObject private var model: AppModel
    let offer: MigrationOffer
    @State private var choices = Migration.Choices()
    @State private var busy = false
    @State private var lines: [String] = []
    @State private var done = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("bodh is already set up on this Mac").font(.title2).fontWeight(.semibold)
            Text("A project was found at \(offer.source.path). The app can take it over, so the menu bar replaces the launchd jobs and the keys move from .env into your Keychain. Nothing is deleted; Settings › Advanced › Undo migration moves it back.")
                .foregroundStyle(.secondary)
            if !done {
                Toggle("Move \(offer.source.lastPathComponent) into \(Paths.applicationSupport.path)", isOn: $choices.move)
                Toggle("Import .env into the Keychain and delete the file", isOn: $choices.importEnv).disabled(!offer.hasEnv)
                Toggle("Remove the launchd jobs (bodh.run, bodh.weekly, bodh.report, bodh.console)", isOn: $choices.removeJobs).disabled(!offer.hasLaunchdJobs)
                Toggle("Start bodh at login", isOn: $choices.loginItem)
                Text("Keep using it in place: untick the first line and set BODH_HOME to \(offer.source.path) before launching the app.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if !lines.isEmpty {
                ScrollView {
                    Text(lines.joined(separator: "\n"))
                        .font(.system(.callout, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: 180)
                .padding(8)
                .background(.quaternary.opacity(0.3))
                .clipShape(RoundedRectangle(cornerRadius: 6))
            }
            HStack {
                Spacer()
                if busy { ProgressView().controlSize(.small) }
                if done {
                    Button("Continue") { model.migration = nil; model.retryEngine() }.keyboardShortcut(.defaultAction)
                } else {
                    Button("Not now") { model.migration = nil; model.retryEngine() }
                    Button("Continue") { Task { await run() } }.keyboardShortcut(.defaultAction).disabled(busy)
                }
            }
        }
        .padding(28)
        .frame(maxWidth: 640)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func run() async {
        busy = true
        defer { busy = false }
        lines = Migration.run(offer, choices: choices)
        if choices.loginItem { model.loginItem.set(true) }
        if choices.importEnv {
            UserDefaults.standard.set(true, forKey: "migration.importEnvPending")
            lines.append("the .env import happens once the engine is running")
        }
        done = true
    }
}
