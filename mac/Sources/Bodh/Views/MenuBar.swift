import AppKit
import SwiftUI
import BodhEngine

/// The menu bar icon: a template symbol that says idle, running, paused or "something needs
/// you", with the unread count beside it when Settings asks for it.
@MainActor struct MenuBarLabel: View {
    @EnvironmentObject private var model: AppModel
    @ObservedObject private var requests = OpenRequests.main
    @Environment(\.openWindow) private var openWindow
    @AppStorage("menubar.showCount") private var showCount = true

    var body: some View {
        HStack(spacing: 4) {
            Image(systemName: model.menuBarSymbol)
                .accessibilityLabel(Text(model.sentence))
            if showCount, let unread = model.unread, model.engineState.isReady {
                Text(unread.formatted())
                    .monospacedDigit()
            }
        }
        .onChange(of: requests.count) { _, _ in
            openWindow(id: "main")
            NSApp.activate(ignoringOtherApps: true)
        }
    }
}

/// The menu: the state in a sentence, the counts, then the levers, the window, BazQux,
/// Settings, updates, Quit.
@MainActor struct MenuBarMenu: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.openWindow) private var openWindow

    // In groups: the macOS 14 SDK's view builder takes ten children at most (the macOS 15 SDK
    // takes any number); a Group is invisible in a menu.
    var body: some View {
        Group {
            Text(model.sentence)
            if let next = model.nextRunText { Text(next) }
            if let unread = model.unread, model.engineState.isReady {
                Text("\(unread.formatted()) unread")
            }
            if let worst = model.worstAttention, worst.level != "good" {
                Text(worst.word)
            }
        }
        Divider()
        Group {
            Button("Run now") { model.runNow() }
                .disabled(!model.engineState.isReady || model.isRunning || model.childRunning)
            Button("Stop") { model.stop() }
                .disabled(!(model.isRunning || model.childRunning))
            Menu("Pause") {
                Button("For an hour") { model.pause(minutes: 60) }
                Button("Until tomorrow morning") { model.pauseUntilTomorrow() }
                Button("Until resumed") { model.pause(minutes: 60 * 24 * 3650) }
                Divider()
                Button("Resume") { model.resume() }.disabled(!model.isPaused)
            }
            .disabled(!model.engineState.isReady)
        }
        Divider()
        Group {
            Button("Open bodh") {
                openWindow(id: "main")
                NSApp.activate(ignoringOtherApps: true)
            }
            .keyboardShortcut("o", modifiers: [.command])
            Button("Open today's list in BazQux") { model.openTodayInBazQux() }
            SettingsLink { Text("Settings…") }
                .keyboardShortcut(",", modifiers: [.command])
            Button("Check for updates…") { Updates.check() }
        }
        Divider()
        Button("Quit bodh") { NSApp.terminate(nil) }
            .keyboardShortcut("q", modifiers: [.command])
    }
}

/// Updates: Sparkle comes with Stage C; until then the releases page.
enum Updates {
    static let releases = URL(string: "https://github.com/abhigyan-j-singh/bodh/releases")!

    static func check() {
        NSWorkspace.shared.open(releases)
    }
}
