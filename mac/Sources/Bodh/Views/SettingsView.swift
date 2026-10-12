import AppKit
import SwiftUI
import BodhEngine

/// Settings in the System Settings shape: the panes on the left, one on the right. Most panes
/// are drawn from the engine's settings schema; Account, Folders, Providers and keys, General,
/// Notifications and Advanced are built by hand around the same store.
enum Pane: String, CaseIterable, Identifiable {
    case general, account, folders, models, providers, judging, cutting, learning, schedule, notifications, connections, data, advanced

    var id: String { rawValue }

    var title: String {
        switch self {
        case .general: return "General"
        case .account: return "Account"
        case .folders: return "Folders"
        case .models: return "Models"
        case .providers: return "Providers and keys"
        case .judging: return "Judging"
        case .cutting: return "Cutting"
        case .learning: return "Learning"
        case .schedule: return "Schedule"
        case .notifications: return "Notifications"
        case .connections: return "Connections"
        case .data: return "Privacy and data"
        case .advanced: return "Advanced"
        }
    }

    var symbol: String {
        switch self {
        case .general: return "gearshape"
        case .account: return "person.crop.circle"
        case .folders: return "folder"
        case .models: return "cpu"
        case .providers: return "key"
        case .judging: return "scalemass"
        case .cutting: return "scissors"
        case .learning: return "brain.head.profile"
        case .schedule: return "clock"
        case .notifications: return "bell"
        case .connections: return "link"
        case .data: return "lock.shield"
        case .advanced: return "wrench.and.screwdriver"
        }
    }

    /// The schema group behind a generic pane.
    var group: String? {
        switch self {
        case .models: return "models"
        case .judging: return "judging"
        case .cutting: return "cutting"
        case .learning: return "learning"
        case .schedule: return "schedule"
        case .connections: return "connections"
        case .data: return "data"
        default: return nil
        }
    }
}

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel
    @StateObject private var store = SettingsStore()
    @State private var pane: Pane? = .general
    @State private var search = ""

    var body: some View {
        NavigationSplitView {
            List(selection: $pane) {
                ForEach(filteredPanes) { item in
                    Label(item.title, systemImage: item.symbol).tag(item)
                }
            }
            .searchable(text: $search, placement: .sidebar, prompt: "Find a setting")
            .navigationSplitViewColumnWidth(min: 180, ideal: 200, max: 240)
        } detail: {
            if let error = store.loadError {
                VStack(spacing: 10) {
                    Text("Settings could not be read from the engine.").font(.title3)
                    Text(error).font(.callout).foregroundStyle(.secondary)
                    Button("Try again") { Task { if let c = model.client { await store.load(c) } } }
                }
                .padding(30)
            } else if store.schema == nil {
                ProgressView("Reading the settings…").padding(30)
            } else if !search.isEmpty {
                SearchResults(query: search)
            } else {
                paneView(pane ?? .general)
            }
        }
        .environmentObject(store)
        .frame(minWidth: 760, minHeight: 520)
        .task(id: model.engineState.port) {
            if let client = model.client { await store.load(client) }
        }
    }

    private var filteredPanes: [Pane] {
        Pane.allCases
    }

    @ViewBuilder
    private func paneView(_ pane: Pane) -> some View {
        switch pane {
        case .general: GeneralPane()
        case .account: AccountPane()
        case .folders: FoldersPane()
        case .providers: ProvidersPane()
        case .notifications: NotificationsPane()
        case .advanced: AdvancedPane()
        default:
            let group = store.schema?.groups.first { $0.key == pane.group }
            GenericPane(group: pane.group ?? "", title: pane.title, help: group?.help ?? "")
        }
    }
}

/// Every setting whose title, help or key matches the search, whatever its pane.
struct SearchResults: View {
    @EnvironmentObject private var store: SettingsStore
    let query: String

    var body: some View {
        let needle = query.lowercased()
        let hits = (store.schema?.settings ?? []).filter {
            $0.control != "folders" && ($0.title.lowercased().contains(needle) || $0.help.lowercased().contains(needle) || $0.key.lowercased().contains(needle))
        }
        Form {
            if hits.isEmpty {
                Text("No setting matches “\(query)”.").foregroundStyle(.secondary)
            }
            ForEach(hits) { setting in
                Section(header: Text(store.schema?.groups.first { $0.key == setting.group }?.title ?? setting.group)) {
                    SettingRow(setting: setting)
                }
            }
        }
        .formStyle(.grouped)
        .navigationTitle("Search")
    }
}

// MARK: General

struct GeneralPane: View {
    @EnvironmentObject private var model: AppModel
    @AppStorage("menubar.showCount") private var showCount = true
    @AppStorage("window.stayClosedAtLaunch") private var stayClosed = false
    @AppStorage("dock.show") private var showInDock = false
    @State private var loginEnabled = false
    @State private var loginNeedsApproval = false
    @State private var loginError: String?

    var body: some View {
        Form {
            Section("Start") {
                Toggle("Start bodh at login", isOn: Binding(
                    get: { loginEnabled },
                    set: { on in
                        model.loginItem.set(on)
                        readLoginItem()
                    }
                ))
                if loginNeedsApproval {
                    Text("macOS wants your approval: System Settings › General › Login Items.").font(.caption).foregroundStyle(.secondary)
                }
                if let error = loginError { Text(error).font(.caption).foregroundStyle(.red) }
                Toggle("Stay in the menu bar at launch (do not open the window)", isOn: $stayClosed)
            }
            Section("Menu bar") {
                Toggle("Show the unread count beside the icon", isOn: $showCount)
                Toggle("Show bodh in the Dock as well", isOn: $showInDock)
                    .onChange(of: showInDock) { _, on in
                        NSApp.setActivationPolicy(on ? .regular : .accessory)
                    }
            }
            Section("Updates") {
                HStack {
                    Text("Updates arrive through the releases page until Sparkle ships.")
                        .foregroundStyle(.secondary).font(.callout)
                    Spacer()
                    Button("Check for updates…") { Updates.check() }
                }
            }
        }
        .formStyle(.grouped)
        .navigationTitle("General")
        .onAppear { readLoginItem() }
    }

    private func readLoginItem() {
        model.loginItem.refresh()
        loginEnabled = model.loginItem.enabled
        loginNeedsApproval = model.loginItem.needsApproval
        loginError = model.loginItem.lastError
    }
}

// MARK: Account

struct AccountPane: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var store: SettingsStore
    @State private var email = ""
    @State private var password = ""
    @State private var busy = false
    @State private var message: String?
    @State private var ok = false

    var body: some View {
        Form {
            Section("BazQux") {
                if let row = store.keys?.row("bazqux") {
                    LabeledContent("Sign-in") {
                        Text(row.set ? "stored in the Keychain" : "not set").foregroundStyle(row.set ? .primary : .secondary)
                    }
                }
                TextField("Email", text: $email).autocorrectionDisabled()
                SecureField("Password", text: $password)
                HStack {
                    Button("Check") { Task { await check() } }.disabled(email.isEmpty || password.isEmpty || busy)
                    Button("Save to the Keychain") { Task { await save() } }.disabled(!ok || busy)
                    if busy { ProgressView().controlSize(.small) }
                    Spacer()
                    if store.keys?.row("bazqux")?.set == true {
                        Button("Remove") { Task { await remove() } }
                    }
                }
                if let message = message { Text(message).font(.callout).foregroundStyle(.secondary) }
            }
            Section("Protection") {
                if let setting = store.setting("protect.must_read_stream") { SettingRow(setting: setting) }
                Text("Create the stream in BazQux: Latest › search for what must never be cut › New smart stream, with this name.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .navigationTitle("Account")
    }

    private func check() async {
        guard let client = store.client else { return }
        busy = true
        defer { busy = false }
        if let result = try? await client.checkReader(email: email, password: password) {
            ok = result.ok
            message = result.ok
                ? "\(result.message)\(result.mustReadStreamPresent == true ? "; the must-read stream is there" : "; the must-read stream was not found")"
                : result.message
        } else {
            message = "The engine did not answer."
        }
    }

    private func save() async {
        guard let client = store.client else { return }
        busy = true
        defer { busy = false }
        do {
            _ = try await client.setReader(email: email, password: password)
            message = "Saved; the next run signs in with it."
            password = ""
            await store.reloadKeys()
            await model.refresh()
        } catch {
            message = "\(error)"
        }
    }

    private func remove() async {
        guard let client = store.client else { return }
        _ = try? await client.deleteKey(provider: "bazqux")
        await store.reloadKeys()
        message = "Removed from the Keychain."
    }
}

// MARK: Folders

struct FoldersPane: View {
    @EnvironmentObject private var store: SettingsStore
    @State private var loading = false
    @State private var message: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(store.schema?.groups.first { $0.key == "folders" }?.help ?? "").font(.callout).foregroundStyle(.secondary)
            FolderTable(rows: $store.folders)
            HStack {
                Button("Read the folders from BazQux") { Task { await load(live: true) } }.disabled(loading)
                if loading { ProgressView().controlSize(.small) }
                Spacer()
                if let message = message { Text(message).font(.callout).foregroundStyle(.secondary) }
                Button("Apply") { Task { await apply() } }.keyboardShortcut(.defaultAction)
            }
            Text("A folder not listed in config.yaml is judged and cut at the default cut-off; the table shows what is in effect. Folders that no longer exist in the account are kept until you remove their line.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding(20)
        .navigationTitle("Folders")
        .task { if store.folders.isEmpty { await load(live: false) } }
    }

    private func load(live: Bool) async {
        loading = true
        await store.loadFolders(live: live)
        loading = false
        message = live ? "Read from BazQux." : nil
    }

    private func apply() async {
        var policies: [String: JSONValue] = [:]
        for row in store.folders where !row.judge || row.protected || row.cutoff != nil {
            var policy: [String: JSONValue] = ["judge": .bool(row.judge), "protected": .bool(row.protected)]
            if let cutoff = row.cutoff { policy["cutoff"] = .number(Double(cutoff)) }
            policies[row.name] = .object(policy)
        }
        let ok = await store.set("folders", .object(policies))
        message = ok ? "Applied; the next run reads it." : (store.errors["folders"] ?? "refused")
        await store.loadFolders(live: false)
    }
}

// MARK: Providers and keys

struct ProvidersPane: View {
    @EnvironmentObject private var store: SettingsStore

    private struct ProviderInfo: Identifiable {
        var id: String
        var name: String
        var page: String
    }

    private let providers: [ProviderInfo] = [
        ProviderInfo(id: "anthropic", name: "Anthropic", page: "https://console.anthropic.com/settings/keys"),
        ProviderInfo(id: "openai", name: "OpenAI", page: "https://platform.openai.com/api-keys"),
        ProviderInfo(id: "gemini", name: "Google Gemini", page: "https://aistudio.google.com/apikey"),
        ProviderInfo(id: "openrouter", name: "OpenRouter", page: "https://openrouter.ai/keys"),
        ProviderInfo(id: "openai-compatible", name: "An OpenAI-compatible server (Ollama and friends)", page: ""),
    ]

    var body: some View {
        Form {
            Section("Keys") {
                ForEach(providers) { p in
                    ProviderRow(provider: p.id, name: p.name, page: p.page)
                }
                if store.keys?.envFile == true {
                    EnvImportRow()
                }
            }
            Section("Budget and prices") {
                ForEach((store.schema?.settings(in: "providers") ?? []).filter { !$0.advanced }) { SettingRow(setting: $0) }
                Text("At the budget, judging pauses until midnight and nothing is cut because of it.").font(.caption).foregroundStyle(.secondary)
            }
            Section {
                DisclosureGroup("Advanced") {
                    ForEach((store.schema?.settings(in: "providers") ?? []).filter { $0.advanced }) { SettingRow(setting: $0) }
                }
            }
        }
        .formStyle(.grouped)
        .navigationTitle("Providers and keys")
    }
}

struct ProviderRow: View {
    @EnvironmentObject private var store: SettingsStore
    let provider: String
    let name: String
    let page: String
    @State private var key = ""
    @State private var baseURL = ""
    @State private var busy = false
    @State private var message: String?

    private var row: KeyRow? { store.keys?.row(provider) }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(name).fontWeight(.medium)
                Spacer()
                if let row = row {
                    Text(row.set ? "set (\(row.variables.first?.source ?? "stored"))" : "not set")
                        .font(.callout).foregroundStyle(row.set ? Color.primary : Color.secondary)
                    if row.required { Text("needed").font(.caption).foregroundStyle(.orange) }
                }
            }
            HStack {
                if provider == "openai-compatible" {
                    TextField("Server URL", text: $baseURL, prompt: Text("http://localhost:11434/v1")).textFieldStyle(.roundedBorder)
                    SecureField("API key (optional)", text: $key).textFieldStyle(.roundedBorder)
                } else {
                    SecureField("API key", text: $key).textFieldStyle(.roundedBorder)
                }
                Button("Check") { Task { await check() } }.disabled(busy || (provider == "openai-compatible" ? baseURL.isEmpty : key.isEmpty))
                Button("Save") { Task { await save() } }.disabled(busy || (provider == "openai-compatible" ? baseURL.isEmpty : key.isEmpty))
                if row?.set == true { Button("Remove") { Task { await remove() } } }
                if !page.isEmpty, let url = URL(string: page) { Link("Get a key", destination: url).font(.callout) }
            }
            if let message = message { Text(message).font(.caption).foregroundStyle(.secondary) }
        }
        .padding(.vertical, 4)
    }

    private func check() async {
        guard let client = store.client else { return }
        busy = true
        defer { busy = false }
        let secret = provider == "openai-compatible" ? baseURL : key
        if let result = try? await client.checkProvider(provider: provider, key: secret, model: nil) {
            message = result.ok ? "\(result.message) (about \(String(format: "%.3f", result.cents ?? 0)) cents)" : result.message
        } else {
            message = "The engine did not answer."
        }
    }

    private func save() async {
        guard let client = store.client else { return }
        busy = true
        defer { busy = false }
        do {
            if provider == "openai-compatible" {
                _ = try await client.setLocalService(baseURL: baseURL, apiKey: key)
            } else {
                _ = try await client.setKey(provider: provider, key: key)
            }
            key = ""
            message = "Saved to the Keychain."
            await store.reloadKeys()
        } catch {
            message = "\(error)"
        }
    }

    private func remove() async {
        guard let client = store.client else { return }
        _ = try? await client.deleteKey(provider: provider)
        await store.reloadKeys()
        message = "Removed."
    }
}

struct EnvImportRow: View {
    @EnvironmentObject private var store: SettingsStore
    @State private var message: String?

    var body: some View {
        HStack {
            VStack(alignment: .leading) {
                Text("A .env file holds keys beside config.yaml.").fontWeight(.medium)
                Text("Move them into the Keychain and delete the file; the engine reads both places, the Keychain first.").font(.caption).foregroundStyle(.secondary)
                if let message = message { Text(message).font(.caption).foregroundStyle(.secondary) }
            }
            Spacer()
            Button("Import and delete .env") {
                Task {
                    guard let client = store.client else { return }
                    do {
                        let keys = try await client.importEnv(delete: true)
                        message = "Imported: \(keys.keys.filter { $0.set }.map { $0.provider }.joined(separator: ", "))"
                        await store.reloadKeys()
                    } catch {
                        message = "\(error)"
                    }
                }
            }
        }
    }
}

// MARK: Notifications

struct NotificationsPane: View {
    @EnvironmentObject private var model: AppModel
    @AppStorage("notify.run_failed") private var runFailed = true
    @AppStorage("notify.budget") private var budget = true
    @AppStorage("notify.weekly") private var weekly = true
    @AppStorage("notify.safety") private var safety = true
    @AppStorage("notify.signin") private var signin = true
    @AppStorage("notify.provider") private var provider = true
    @AppStorage("notify.failed") private var failed = true
    @AppStorage("notify.sound") private var sound = true

    var body: some View {
        Form {
            Section("Tell me when") {
                Toggle("A run failed", isOn: $runFailed)
                Toggle("The daily budget is reached", isOn: $budget)
                Toggle("The Sunday rewrite changed the profile", isOn: $weekly)
                Toggle("The safety limit tripped", isOn: $safety)
                Toggle("BazQux refused the sign-in", isOn: $signin)
                Toggle("The model provider refused the key", isOn: $provider)
                Toggle("Something else went wrong in a run", isOn: $failed)
            }
            Section {
                Toggle("Play a sound", isOn: $sound)
                HStack {
                    Button("Send a test notification") { model.notifications.sendTest() }
                    Button("Ask for permission again") { model.notifications.requestPermission() }
                }
                Text("Clicking a notification opens the window; a sign-in problem opens Settings.").font(.caption).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .navigationTitle("Notifications")
    }
}

// MARK: Advanced

struct AdvancedPane: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var store: SettingsStore
    @State private var message: String?
    @State private var confirmUndo = false

    var body: some View {
        Form {
            Section("Where things are") {
                LabeledContent("Project folder") {
                    HStack {
                        Text(Paths.home.path).textSelection(.enabled).lineLimit(1).truncationMode(.middle)
                        Button("Reveal in Finder") { Reveal.dataFolder() }
                    }
                }
                LabeledContent("config.yaml") {
                    HStack {
                        Text(store.configPath).textSelection(.enabled).lineLimit(1).truncationMode(.middle)
                        Button("Open") { Reveal.configFile() }
                    }
                }
                Text("The app rewrites config.yaml when you change a setting and keeps your comments; a hand edit shows here within a minute.").font(.caption).foregroundStyle(.secondary)
            }
            Section("The command line") {
                HStack {
                    Text(CLIInstaller.isInstalled ? "bodh is on the command line (\(CLIInstaller.link))." : "Put bodh on the command line as \(CLIInstaller.link).")
                    Spacer()
                    Button(CLIInstaller.isInstalled ? "Remove" : "Install command line tool") {
                        message = CLIInstaller.isInstalled ? CLIInstaller.remove() : CLIInstaller.install()
                    }
                }
                Text("In your shell profile: export BODH_HOME=\"\(Paths.home.path)\"").font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
            }
            Section("The engine") {
                LabeledContent("Version", value: model.health?.version ?? "–")
                LabeledContent("State", value: model.engineState.description)
                LabeledContent("Port", value: model.engineState.port.map { String($0) } ?? "–")
                LabeledContent("Up for", value: model.health.map { Clock.minutes($0.uptimeS) } ?? "–")
                HStack {
                    Button("Restart the engine") { model.restartEngine() }
                    Button("Export diagnostics…") { Task { if let url = await Diagnostics.export(model: model) { message = "Written: \(url.lastPathComponent)" } } }
                }
                Text("Diagnostics: the app's log, the engine's log tail, the health and the settings, with every secret left out; nothing is sent anywhere.").font(.caption).foregroundStyle(.secondary)
            }
            Section("Onboarding and migration") {
                HStack {
                    Button("Run onboarding again") { model.resetOnboarding(); WindowOpener.openMain() }
                    if Migration.undoSource != nil {
                        Button("Undo migration…") { confirmUndo = true }
                    }
                }
                if let source = Migration.undoSource {
                    Text("Undo moves the project back to \(source.path) and prints the command that restores the launchd jobs.").font(.caption).foregroundStyle(.secondary)
                }
            }
            if let message = message {
                Section { Text(message).font(.callout).foregroundStyle(.secondary).textSelection(.enabled) }
            }
        }
        .formStyle(.grouped)
        .navigationTitle("Advanced")
        .confirmationDialog("Move the project back and stop using the app's folder?", isPresented: $confirmUndo) {
            Button("Undo migration", role: .destructive) {
                Task {
                    await model.shutdown()
                    message = Migration.undo() + "\nThe app stays stopped: quit it and run bodh from the terminal, or start it again to set up afresh."
                }
            }
            Button("Cancel", role: .cancel) {}
        }
    }
}
