import Foundation
import ServiceManagement

/// Start at login, through the system's own switch (`SMAppService`); Settings shows the
/// system's state, not a stored wish.
final class LoginItem: ObservableObject {
    @Published var enabled: Bool = SMAppService.mainApp.status == .enabled
    @Published var lastError: String?

    func refresh() {
        enabled = SMAppService.mainApp.status == .enabled
    }

    func set(_ on: Bool) {
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
    var needsApproval: Bool { SMAppService.mainApp.status == .requiresApproval }
}
