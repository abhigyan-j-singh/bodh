import AppKit
import SwiftUI
import BodhEngine

/// The first launch, six steps in the window: welcome; BazQux sign-in (checked live); a
/// provider and its key (checked live, cents stated); the five questions that become the
/// student profile; the folders to protect and to skip; scoring-only or cutting. The last
/// screen starts the first run and shows it live. Every answer goes through the engine's
/// service; the app keeps none of it.
@MainActor struct OnboardingView: View {
    @EnvironmentObject private var model: AppModel
    @State private var step = 0
    @State private var busy = false
    @State private var message: String?
    @State private var messageIsError = false

    // the reader
    @State private var email = ""
    @State private var password = ""
    @State private var readerOK = false
    // the provider
    @State private var provider = "anthropic"
    @State private var key = ""
    @State private var baseURL = ""
    @State private var modelID = "claude-sonnet-5-5"
    @State private var providerOK = false
    // the profile
    @State private var answers: [String: String] = [:]
    @State private var questions: [ProfileQuestion] = []
    // the folders
    @State private var folders: [FolderRow] = []
    @State private var foldersLoaded = false
    // the mode
    @State private var cutting = false
    @State private var startedRun = false

    private let providers = ["anthropic", "openai", "gemini", "openrouter", "openai-compatible"]
    private let titles = ["Welcome", "Your reader", "A model", "About you", "Your folders", "Scoring or cutting"]

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                ForEach(titles.indices, id: \.self) { i in
                    Text(titles[i])
                        .font(.caption)
                        .fontWeight(i == step ? .semibold : .regular)
                        .foregroundStyle(i == step ? Color.primary : Color.secondary)
                    if i < titles.count - 1 { Image(systemName: "chevron.right").font(.caption2).foregroundStyle(.tertiary) }
                }
                Spacer()
            }
            .padding(.horizontal, 28)
            .padding(.top, 18)
            Divider().padding(.top, 12)
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    content
                }
                .padding(28)
                .frame(maxWidth: 640, alignment: .leading)
            }
            Divider()
            HStack {
                if let message = message {
                    Label(message, systemImage: messageIsError ? "exclamationmark.triangle" : "checkmark.circle")
                        .foregroundStyle(messageIsError ? Color.orange : Color.secondary)
                        .font(.callout)
                        .lineLimit(2)
                }
                Spacer()
                if busy { ProgressView().controlSize(.small).padding(.trailing, 8) }
                if step > 0 && !startedRun {
                    Button("Back") { message = nil; step -= 1 }
                }
                Button(step == titles.count - 1 ? (startedRun ? "Done" : "Start the first run") : "Continue") {
                    Task { await next() }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(busy || !canContinue)
            }
            .padding(.horizontal, 28)
            .padding(.vertical, 14)
        }
        .task { await load() }
    }

    private var canContinue: Bool {
        switch step {
        case 1: return readerOK
        case 2: return providerOK
        case 3: return questions.allSatisfy { !(answers[$0.key] ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        default: return true
        }
    }

    @ViewBuilder
    private var content: some View {
        switch step {
        case 0: welcome
        case 1: reader
        case 2: providerStep
        case 3: profile
        case 4: foldersStep
        default: modeStep
        }
    }

    // MARK: the steps

    private var welcome: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("bodh keeps your BazQux unread list worth reading.").font(.title2).fontWeight(.semibold)
            Text("Every half hour it reads what arrived the way a teacher judges reading material for one student: the quality of the writing, what you would learn, the fit with what you know. It tags every article with a score band in BazQux, keeps a reading list of the best, and, when you let it, marks the worst read. It learns from what you star, read and grade.")
            Text("Setting up takes a few minutes: your BazQux login, a model provider and its key, five questions about you, your folders, and whether it may cut. Nothing is cut until you say so.")
                .foregroundStyle(.secondary)
        }
    }

    private var reader: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Sign in to BazQux").font(.title2).fontWeight(.semibold)
            Text("The same email and password as bazqux.com. They are kept in your Keychain by the engine; the app never sees them again.").foregroundStyle(.secondary)
            Form {
                TextField("Email", text: $email).textContentType(.username).autocorrectionDisabled()
                SecureField("Password", text: $password)
            }
            .formStyle(.grouped)
            .onChange(of: email) { _, _ in readerOK = false }
            .onChange(of: password) { _, _ in readerOK = false }
            Button("Check the sign-in") { Task { await checkReader() } }
                .disabled(email.isEmpty || password.isEmpty || busy)
        }
    }

    private var providerStep: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("A model provider").font(.title2).fontWeight(.semibold)
            Text("The judge reads every article through this provider. Anthropic is the one the engine was tuned with; any of the others works with a key and a model id (provider/model-id).").foregroundStyle(.secondary)
            Form {
                Picker("Provider", selection: $provider) {
                    ForEach(providers, id: \.self) { Text($0).tag($0) }
                }
                .onChange(of: provider) { _, new in
                    providerOK = false
                    modelID = new == "anthropic" ? "claude-sonnet-5-5" : ""
                }
                if provider == "openai-compatible" {
                    TextField("Server URL", text: $baseURL, prompt: Text("http://localhost:11434/v1"))
                    SecureField("API key (if the server needs one)", text: $key)
                } else {
                    SecureField("API key", text: $key)
                }
                TextField("Model id", text: $modelID, prompt: Text(provider == "anthropic" ? "claude-sonnet-5-5" : "\(provider)/model-id"))
                    .autocorrectionDisabled()
            }
            .formStyle(.grouped)
            .onChange(of: key) { _, _ in providerOK = false }
            .onChange(of: modelID) { _, _ in providerOK = false }
            Button("Check with one small call") { Task { await checkProvider() } }
                .disabled((provider == "openai-compatible" ? baseURL.isEmpty : key.isEmpty) || busy)
            Text("The check costs a fraction of a cent; the amount is stated.").font(.caption).foregroundStyle(.secondary)
        }
    }

    private var profile: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("About you").font(.title2).fontWeight(.semibold)
            Text("Five short answers become the student profile the judge reads with every article. Two or three sentences each is plenty; you can edit them later in Settings.").foregroundStyle(.secondary)
            ForEach(questions) { q in
                VStack(alignment: .leading, spacing: 4) {
                    Text(q.question).font(.callout).fontWeight(.medium)
                    TextEditor(text: Binding(get: { answers[q.key] ?? "" }, set: { answers[q.key] = $0 }))
                        .font(.body)
                        .frame(minHeight: 60)
                        .overlay(RoundedRectangle(cornerRadius: 5).stroke(.quaternary))
                        .accessibilityLabel(q.question)
                }
            }
        }
    }

    private var foldersStep: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Your folders").font(.title2).fontWeight(.semibold)
            Text("Judge: sent to the judge at all. Never cut: stays unread whatever the score. A folder with both off is never read by the judge; comics, say. The rest can be set later, folder by folder, in Settings.").foregroundStyle(.secondary)
            if !foldersLoaded {
                ProgressView("Reading your folders from BazQux…")
            } else if folders.isEmpty {
                Text("No folders yet. BazQux folders appear here once the first run has fetched articles.").foregroundStyle(.secondary)
            } else {
                FolderTable(rows: $folders)
            }
        }
    }

    private var modeStep: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Scoring only, or cutting").font(.title2).fontWeight(.semibold)
            Picker("", selection: $cutting) {
                Text("Scoring only (recommended to start): judge and tag every article, cut nothing").tag(false)
                Text("Cutting: mark what scores under the cut-off read, within the safety limits").tag(true)
            }
            .pickerStyle(.radioGroup)
            .labelsHidden()
            Text("A few days of scoring only show you how the judge thinks; turn cutting on in Settings when the bands look right. Every cut can be undone.").foregroundStyle(.secondary)
            if startedRun {
                Divider()
                Label(model.sentence, systemImage: model.isRunning || model.childRunning ? "circle.dotted" : "checkmark.circle")
                    .font(.callout)
                if let unread = model.unread { Text("\(unread) unread").font(.callout).foregroundStyle(.secondary) }
                Text("The first run judges what arrived in the last two weeks; the reading list fills in as the judgments land. You can close this and come back any time.").font(.callout).foregroundStyle(.secondary)
            }
        }
    }

    // MARK: the calls

    private func load() async {
        guard let client = model.client else { return }
        if let prof = try? await client.profile() {
            questions = prof.questions
        }
        if let keys = try? await client.keys(), keys.row("bazqux")?.set == true {
            readerOK = true
            email = "(signed in)"
            password = "(stored)"
        }
    }

    private func checkReader() async {
        guard let client = model.client else { return }
        busy = true
        defer { busy = false }
        do {
            let check = try await client.checkReader(email: email, password: password)
            readerOK = check.ok
            say(check.message, error: !check.ok)
        } catch {
            say("\(error)", error: true)
        }
    }

    private func checkProvider() async {
        guard let client = model.client else { return }
        busy = true
        defer { busy = false }
        do {
            let secret = provider == "openai-compatible" ? baseURL : key  // a local service is named by its URL
            let check = try await client.checkProvider(provider: provider, key: secret, model: modelID.isEmpty ? nil : modelID)
            providerOK = check.ok
            if let cents = check.cents, check.ok {
                say("\(check.message) (about \(String(format: "%.3f", cents)) cents)", error: false)
            } else {
                say(check.message, error: !check.ok)
            }
            if let modelName = check.model, modelID.isEmpty { modelID = modelName }
        } catch {
            say("\(error)", error: true)
        }
    }

    private func next() async {
        guard let client = model.client else { return }
        busy = true
        defer { busy = false }
        do {
            switch step {
            case 1:
                if email != "(signed in)" {
                    _ = try await client.setReader(email: email, password: password)
                }
            case 2:
                if provider == "openai-compatible" {
                    _ = try await client.setLocalService(baseURL: baseURL, apiKey: key)
                } else {
                    _ = try await client.setKey(provider: provider, key: key)
                }
                if provider != "anthropic", !modelID.isEmpty {
                    let m = JSONValue.string(modelID)
                    _ = try await client.update(settings: ["models.judge": m, "models.reviewer": m, "models.profiler": m, "catch_up.model": m])
                } else if provider == "anthropic", !modelID.isEmpty, modelID != "claude-sonnet-5-5" {
                    _ = try await client.update(settings: ["models.judge": .string(modelID)])
                }
            case 3:
                _ = try await client.updateProfile(answers: answers)
            case 4:
                var policies: [String: JSONValue] = [:]
                for row in folders where !row.judge || row.protected || row.cutoff != nil {
                    var policy: [String: JSONValue] = ["judge": .bool(row.judge), "protected": .bool(row.protected)]
                    if let cutoff = row.cutoff { policy["cutoff"] = .number(Double(cutoff)) }
                    policies[row.name] = .object(policy)
                }
                _ = try await client.update(settings: ["folders": .object(policies)])
            case 5:
                if startedRun {
                    model.finishOnboarding()
                    return
                }
                _ = try await client.update(settings: ["autocut.enabled": .bool(cutting)])
                model.notifications.requestPermission()
                model.loginItem.set(true)
                _ = try await client.run(mode: "normal")
                startedRun = true
                await model.refresh()
                return
            default:
                break
            }
            message = nil
            step += 1
            if step == 4 && !foldersLoaded {
                await loadFolders()
            }
            if step == 1 && readerOK { message = "Already signed in; Continue keeps it." }
        } catch {
            say("\(error)", error: true)
        }
    }

    private func loadFolders() async {
        guard let client = model.client else { return }
        if let answer = try? await client.folders(live: true) {
            folders = answer.folders
        } else if let answer = try? await client.folders(live: false) {
            folders = answer.folders
        }
        foldersLoaded = true
    }

    private func say(_ text: String, error: Bool) {
        message = text
        messageIsError = error
    }
}

/// The folders as a table: name, articles a day, Judge, Never cut, the cut-off.
@MainActor struct FolderTable: View {
    @Binding var rows: [FolderRow]

    var body: some View {
        Table($rows) {
            TableColumn("Folder") { row in Text(row.wrappedValue.name) }
            TableColumn("A day") { row in
                Text(row.wrappedValue.articlesPerDay, format: .number.precision(.fractionLength(1)))
                    .foregroundStyle(.secondary)
            }
            .width(60)
            TableColumn("Judge") { row in
                Toggle("", isOn: row.judge).labelsHidden().accessibilityLabel("Judge \(row.wrappedValue.name)")
            }
            .width(50)
            TableColumn("Never cut") { row in
                Toggle("", isOn: row.protected).labelsHidden().accessibilityLabel("Never cut \(row.wrappedValue.name)")
            }
            .width(70)
            TableColumn("Cut-off") { row in
                TextField("default", value: row.cutoff, format: .number)
                    .frame(width: 60)
                    .accessibilityLabel("Cut-off for \(row.wrappedValue.name)")
            }
            .width(80)
        }
        .frame(minHeight: 220)
    }
}
