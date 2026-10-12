import AppKit
import Foundation
import UserNotifications

/// The few notifications that deserve one: a failed run, the budget, the Sunday rewrite, the
/// safety limit, a sign-in that failed. Each event is a switch in Settings; a click opens the
/// window (and Settings for a sign-in). Nothing is sent twice for the same condition.
final class Notifications: NSObject, UNUserNotificationCenterDelegate {
    enum Kind {
        case runFailed
        case attention(String)

        var preferenceKey: String {
            switch self {
            case .runFailed: return "notify.run_failed"
            case .attention(let key): return "notify." + key
            }
        }
    }

    static let defaults: [String: Bool] = [
        "notify.run_failed": true, "notify.budget": true, "notify.weekly": true, "notify.safety": true,
        "notify.signin": true, "notify.provider": true, "notify.failed": true,
    ]

    private var sent: Set<String> = Set(UserDefaults.standard.stringArray(forKey: "notifications.sent") ?? [])
    var onOpen: (@MainActor (Kind) -> Void)?

    override init() {
        super.init()
        UNUserNotificationCenter.current().delegate = self
    }

    static func isEnabled(_ key: String) -> Bool {
        if UserDefaults.standard.object(forKey: key) == nil { return defaults[key] ?? false }
        return UserDefaults.standard.bool(forKey: key)
    }

    func requestPermission() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, error in
            AppLog.info("notifications: \(granted ? "allowed" : "declined")\(error.map { " (\($0.localizedDescription))" } ?? "")")
        }
    }

    func send(id: String, title: String, body: String, kind: Kind) {
        guard Notifications.isEnabled(kind.preferenceKey), !sent.contains(id) else { return }
        sent.insert(id)
        if sent.count > 500 { sent = Set(sent.suffix(300)) }
        UserDefaults.standard.set(Array(sent), forKey: "notifications.sent")
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = UserDefaults.standard.object(forKey: "notify.sound") == nil || UserDefaults.standard.bool(forKey: "notify.sound") ? .default : nil
        content.userInfo = ["kind": kind.preferenceKey]
        let request = UNNotificationRequest(identifier: id, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request) { error in
            if let error = error { AppLog.warning("notification not shown: \(error.localizedDescription)") }
        }
    }

    func sendTest() {
        let content = UNMutableNotificationContent()
        content.title = "bodh"
        content.body = "This is what a notification looks like."
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }

    // The app is in front: show the banner anyway.
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification, withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse, withCompletionHandler completionHandler: @escaping () -> Void) {
        let key = response.notification.request.content.userInfo["kind"] as? String ?? ""
        let kind: Kind = key == "notify.run_failed" ? .runFailed : .attention(String(key.dropFirst("notify.".count)))
        let open = onOpen
        Task { @MainActor in open?(kind) }
        completionHandler()
    }
}
