import Foundation
import ServiceManagement

/// Start at login, through the system's own switch (`SMAppService`); Settings shows the
/// system's state, not a stored wish.
final class LoginItem: ObservableObject {
    @Published var enabled: Bool = LoginItem.available && SMAppService.mainApp.status == .enabled
    @Published var lastError: String?

    /// A login item needs an app bundle; the bare executable of `swift run` has none.
    static let available = Bundle.main.bundleIdentifier != nil

    func refresh() {
        enabled = LoginItem.available && SMAppService.mainApp.status == .enabled
    }

    func set(_ on: Bool) {
        guard LoginItem.available else {
            lastError = "Not available when the app runs outside its bundle."
            return
        }
        do {
            if on {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
            lastError = nil
        } catch {
            lastError = error.localizedDescription
            AppLog.warning("login item: \(error.localizedDescription)")
        }
        refresh()
    }

    /// `requiresApproval` means System Settings holds a switch the user must flip.
    var needsApproval: Bool { LoginItem.available && SMAppService.mainApp.status == .requiresApproval }
}
