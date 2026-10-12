import AppKit
import SwiftUI
import BodhEngine

/// bodh for Mac: a menu bar extra, one window onto the engine, a Settings window. The engine
/// (`bodh serve --app`) is started by the app delegate at launch and stopped when the app quits.
@main
struct BodhApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var model = AppModel.shared

    var body: some Scene {
        MenuBarExtra {
            MenuBarMenu()
                .environmentObject(model)
        } label: {
            MenuBarLabel()
                .environmentObject(model)
        }

        Window("bodh", id: "main") {
            MainWindow()
                .environmentObject(model)
        }
        .defaultSize(width: 980, height: 680)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandMenu("Engine") {
                Button("Run now") { model.runNow() }.keyboardShortcut("r", modifiers: [.command])
                Button("Stop") { model.stop() }.keyboardShortcut(".", modifiers: [.command])
                Divider()
                Button("Restart the engine") { model.restartEngine() }
            }
        }

        Settings {
            SettingsView()
                .environmentObject(model)
        }
    }
}

extension AppModel {
    static let shared = AppModel()
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        AppLog.info("launch: \(Bundle.main.bundleIdentifier ?? "dev") \(Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "")")
        let model = AppModel.shared
        model.notifications.onOpen = { kind in
            WindowOpener.openMain()
            if case .attention(let key) = kind, key == "signin" || key == "provider" {
                WindowOpener.openSettings()
            }
        }
        model.start()
        // launched at login: stay in the menu bar; opened by hand: show the window
        let atLogin = ProcessInfo.processInfo.arguments.contains("--at-login") || NSApp.isHidden
        if !atLogin && !UserDefaults.standard.bool(forKey: "window.stayClosedAtLaunch") {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { WindowOpener.openMain() }
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { WindowOpener.openMain() }
        return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        let model = AppModel.shared
        Task { @MainActor in
            await model.shutdown()
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 8) {
            NSApp.reply(toApplicationShouldTerminate: true)  // never hang on quit
        }
        return .terminateLater
    }
}

/// Opening the window and Settings from outside a view (the delegate, a notification): the
/// scenes answer to their own ids through the responder chain.
@MainActor
enum WindowOpener {
    static func openMain() {
        NSApp.activate(ignoringOtherApps: true)
        if let window = NSApp.windows.first(where: { ($0.identifier?.rawValue ?? "").hasPrefix("main") || $0.title == "bodh" }) {
            window.makeKeyAndOrderFront(nil)
            return
        }
        OpenRequests.main.send()  // the menu bar label view answers with openWindow(id: "main")
    }

    static func openSettings() {
        NSApp.activate(ignoringOtherApps: true)
        if #available(macOS 14, *) {
            NSApp.sendAction(Selector(("showSettingsWindow:")), to: nil, from: nil)
        }
    }
}

/// A view inside the menu bar extra listens for these and calls `openWindow`, which only a
/// view can do.
final class OpenRequests: ObservableObject {
    static let main = OpenRequests()
    @Published var count = 0
    func send() { DispatchQueue.main.async { self.count += 1 } }
}
