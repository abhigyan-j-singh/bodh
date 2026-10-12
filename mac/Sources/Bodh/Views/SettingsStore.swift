import Foundation
import SwiftUI
import BodhEngine

/// The settings as the window edits them: the schema (every key, its control and its pane),
/// the values in effect with their source, and one `set` that is one `PUT /api/settings`,
/// confirmed by what the engine answers; a refusal lands beside the field as its message.
@MainActor
final class SettingsStore: ObservableObject {
    @Published var schema: SettingsSchema?
    @Published var values: [String: JSONValue] = [:]
    @Published var sources: [String: String] = [:]
    @Published var errors: [String: String] = [:]
    @Published var saving: Set<String> = []
    @Published var loadError: String?
    @Published var keys: KeysAnswer?
    @Published var folders: [FolderRow] = []
    @Published var configPath = ""

    private(set) var client: EngineClient?

    func load(_ client: EngineClient) async {
        self.client = client
        do {
            async let schema = client.settingsSchema()
            async let answer = client.settings()
            async let keys = client.keys()
            let (s, a, k) = try await (schema, answer, keys)
            self.schema = s
            values = a.settings
            sources = a.sources
            configPath = a.path
            self.keys = k
            loadError = nil
        } catch {
            loadError = "\(error)"
            AppLog.warning("settings did not load: \(error)")
        }
    }

    func reloadKeys() async {
        guard let client = client else { return }
        keys = try? await client.keys()
    }

    func loadFolders(live: Bool) async {
        guard let client = client else { return }
        if let answer = try? await client.folders(live: live) {
            folders = answer.folders
        }
    }

    func setting(_ key: String) -> Setting? { schema?.settings.first { $0.key == key } }

    func value(_ key: String) -> JSONValue { values[key] ?? setting(key)?.default ?? .null }

    /// One change; returns whether the engine took it.
    @discardableResult
    func set(_ key: String, _ value: JSONValue) async -> Bool {
        guard let client = client else { return false }
        if values[key] == value { errors[key] = nil; return true }
        saving.insert(key)
        defer { saving.remove(key) }
        do {
            let answer = try await client.update(settings: [key: value])
            values = answer.settings
            sources = answer.sources
            errors[key] = nil
            return true
        } catch let error as EngineError {
            let fieldErrors = error.fieldErrors
            errors[key] = fieldErrors[key] ?? fieldErrors.values.first ?? error.description
            return false
        } catch {
            errors[key] = "\(error)"
            return false
        }
    }

    func reset(_ key: String) async {
        guard let setting = setting(key) else { return }
        await set(key, setting.default)
    }

    /// Every setting of a pane back to its default, one request.
    func resetGroup(_ group: String) async {
        guard let client = client, let schema = schema else { return }
        var changes: [String: JSONValue] = [:]
        for s in schema.settings(in: group) where values[s.key] != s.default {
            changes[s.key] = s.default
        }
        guard !changes.isEmpty else { return }
        if let answer = try? await client.update(settings: changes) {
            values = answer.settings
            sources = answer.sources
            for key in changes.keys { errors[key] = nil }
        }
    }
}
