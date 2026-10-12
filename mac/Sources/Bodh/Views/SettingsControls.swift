import SwiftUI
import BodhEngine

/// One setting as a row of a pane, drawn from its `control`: a switch, a number with its
/// unit, a picker, a text field, or one of the small editors. A change applies when the field
/// loses focus or Return is pressed and is confirmed by the engine; a refusal shows under
/// the field in the engine's words. The config key shows on hover for those who want it.
struct SettingRow: View {
    @EnvironmentObject private var store: SettingsStore
    let setting: Setting

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            control
            if let error = store.errors[setting.key] {
                Text(error).font(.caption).foregroundStyle(.red)
            } else if !setting.help.isEmpty {
                Text(setting.help).font(.caption).foregroundStyle(.secondary)
            }
        }
        .help(setting.key + (store.sources[setting.key] == "config.yaml" ? " (set in config.yaml)" : " (the default)"))
        .accessibilityElement(children: .contain)
    }

    @ViewBuilder
    private var control: some View {
        switch setting.control {
        case "switch":
            Toggle(setting.title, isOn: Binding(
                get: { store.value(setting.key).boolValue ?? false },
                set: { new in Task { await store.set(setting.key, .bool(new)) } }
            ))
        case "number":
            NumberRow(setting: setting)
        case "choice":
            Picker(setting.title, selection: Binding(
                get: { store.value(setting.key).stringValue ?? setting.choices?.first ?? "" },
                set: { new in Task { await store.set(setting.key, .string(new)) } }
            )) {
                ForEach(setting.choices ?? [], id: \.self) { Text($0).tag($0) }
            }
        case "weights":
            WeightsRow(setting: setting)
        case "bands":
            BandsRow(setting: setting)
        case "prices":
            PricesRow(setting: setting)
        default:  // text, tag, path, timezone, model
            TextRow(setting: setting)
        }
    }
}

/// A text setting: the draft is committed on Return or when the focus leaves.
struct TextRow: View {
    @EnvironmentObject private var store: SettingsStore
    let setting: Setting
    @State private var draft = ""
    @FocusState private var focused: Bool

    var body: some View {
        HStack {
            TextField(setting.title, text: $draft, prompt: setting.control == "model" ? Text("provider/model-id") : nil)
                .textFieldStyle(.roundedBorder)
                .autocorrectionDisabled()
                .focused($focused)
                .onSubmit { commit() }
                .onChange(of: focused) { _, now in if !now { commit() } }
                .onAppear { draft = store.value(setting.key).stringValue ?? "" }
                .onChange(of: store.values[setting.key]) { _, _ in
                    if !focused { draft = store.value(setting.key).stringValue ?? "" }
                }
                .accessibilityLabel(setting.title)
            if store.saving.contains(setting.key) { ProgressView().controlSize(.small) }
            ResetButton(setting: setting)
        }
    }

    private func commit() {
        let current = store.value(setting.key).stringValue ?? ""
        guard draft != current else { return }
        Task { await store.set(setting.key, .string(draft)) }
    }
}

/// A number with its unit and range; an empty field is "empty" when the setting allows it.
struct NumberRow: View {
    @EnvironmentObject private var store: SettingsStore
    let setting: Setting
    @State private var draft = ""
    @FocusState private var focused: Bool

    var body: some View {
        HStack {
            Text(setting.title)
            Spacer()
            TextField("", text: $draft, prompt: Text(setting.nullable ? "empty" : ""))
                .textFieldStyle(.roundedBorder)
                .multilineTextAlignment(.trailing)
                .frame(width: 90)
                .focused($focused)
                .onSubmit { commit() }
                .onChange(of: focused) { _, now in if !now { commit() } }
                .onAppear { draft = text(store.value(setting.key)) }
                .onChange(of: store.values[setting.key]) { _, _ in if !focused { draft = text(store.value(setting.key)) } }
                .accessibilityLabel(setting.title)
            if let unit = setting.unit { Text(unit).foregroundStyle(.secondary).frame(minWidth: 60, alignment: .leading) }
            if store.saving.contains(setting.key) { ProgressView().controlSize(.small) }
            ResetButton(setting: setting)
        }
    }

    private func text(_ value: JSONValue) -> String {
        switch value {
        case .number(let n): return setting.isInteger ? String(Int(n)) : JSONValue.number(n).description
        case .null: return ""
        default: return value.description
        }
    }

    private func commit() {
        let trimmed = draft.trimmingCharacters(in: .whitespaces)
        let new: JSONValue
        if trimmed.isEmpty {
            guard setting.nullable else { draft = text(store.value(setting.key)); return }
            new = .null
        } else if let n = Double(trimmed) {
            new = .number(setting.isInteger ? n.rounded() : n)
        } else {
            store.errors[setting.key] = "Not a number."
            return
        }
        guard new != store.value(setting.key) else { return }
        Task { await store.set(setting.key, new) }
    }
}

/// Back to the default, shown only when the value differs from it.
struct ResetButton: View {
    @EnvironmentObject private var store: SettingsStore
    let setting: Setting

    var body: some View {
        if store.value(setting.key) != setting.default {
            Button {
                Task { await store.reset(setting.key) }
            } label: {
                Image(systemName: "arrow.counterclockwise")
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .help("Back to the default: \(setting.default.description)")
            .accessibilityLabel("Reset \(setting.title) to its default")
        }
    }
}

/// The five weights, summing to one; committed together when the sum is right.
struct WeightsRow: View {
    @EnvironmentObject private var store: SettingsStore
    let setting: Setting
    @State private var drafts: [String: String] = [:]
    private let order = ["quality", "learning", "fit", "breadth", "relevance"]

    private var sum: Double { order.compactMap { Double(drafts[$0] ?? "") }.reduce(0, +) }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(setting.title)
            HStack(spacing: 8) {
                ForEach(order, id: \.self) { name in
                    VStack(spacing: 2) {
                        TextField(name, text: Binding(get: { drafts[name] ?? "" }, set: { drafts[name] = $0 }))
                            .textFieldStyle(.roundedBorder)
                            .multilineTextAlignment(.center)
                            .frame(width: 64)
                            .onSubmit { commit() }
                            .accessibilityLabel("Weight of \(name)")
                        Text(name).font(.caption2).foregroundStyle(.secondary)
                    }
                }
                VStack(spacing: 2) {
                    Text(String(format: "%.2f", sum))
                        .monospacedDigit()
                        .foregroundStyle(abs(sum - 1) < 0.011 ? Color.secondary : Color.red)
                    Text("sum").font(.caption2).foregroundStyle(.secondary)
                }
                Button("Apply") { commit() }.disabled(abs(sum - 1) >= 0.011)
                ResetButton(setting: setting)
            }
        }
        .onAppear { load() }
        .onChange(of: store.values[setting.key]) { _, _ in load() }
    }

    private func load() {
        let object = store.value(setting.key).objectValue ?? [:]
        for name in order { drafts[name] = object[name].map { JSONValue.number($0.doubleValue ?? 0).description } ?? "0" }
    }

    private func commit() {
        guard abs(sum - 1) < 0.011 else { return }
        var object: [String: JSONValue] = [:]
        for name in order { object[name] = .number(Double(drafts[name] ?? "") ?? 0) }
        Task { await store.set(setting.key, .object(object)) }
    }
}

/// The score bands: threshold and tag name per row, best first.
struct BandsRow: View {
    @EnvironmentObject private var store: SettingsStore
    let setting: Setting
    @State private var rows: [(threshold: String, name: String)] = []

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(setting.title)
                Spacer()
                Button("Apply") { commit() }
                ResetButton(setting: setting)
            }
            ForEach(rows.indices, id: \.self) { i in
                HStack {
                    TextField("score", text: Binding(get: { rows[i].threshold }, set: { rows[i].threshold = $0 }))
                        .frame(width: 60).textFieldStyle(.roundedBorder).accessibilityLabel("Band threshold")
                    Text("and up →").foregroundStyle(.secondary)
                    TextField("tag", text: Binding(get: { rows[i].name }, set: { rows[i].name = $0 }))
                        .frame(width: 140).textFieldStyle(.roundedBorder).accessibilityLabel("Band tag name")
                    Button { rows.remove(at: i) } label: { Image(systemName: "minus.circle") }.buttonStyle(.plain).accessibilityLabel("Remove band")
                }
            }
            Button { rows.append(("", "")) } label: { Label("Add a band", systemImage: "plus.circle") }.buttonStyle(.plain)
            Text("Tags already written in BazQux keep their old names.").font(.caption).foregroundStyle(.secondary)
        }
        .onAppear { load() }
        .onChange(of: store.values[setting.key]) { _, _ in load() }
    }

    private func load() {
        let object = store.value(setting.key).objectValue ?? [:]
        rows = object.keys.sorted { (Int($0) ?? 0) > (Int($1) ?? 0) }.map { ($0, object[$0]?.stringValue ?? "") }
    }

    private func commit() {
        var object: [String: JSONValue] = [:]
        for row in rows where !row.threshold.isEmpty && !row.name.isEmpty { object[row.threshold] = .string(row.name) }
        Task { await store.set(setting.key, .object(object)) }
    }
}

/// The price table: a model per row, input, output and cache read per million tokens.
struct PricesRow: View {
    @EnvironmentObject private var store: SettingsStore
    let setting: Setting
    @State private var rows: [(model: String, prices: [String])] = []

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(setting.title)
                Spacer()
                Button("Apply") { commit() }
                ResetButton(setting: setting)
            }
            HStack {
                Text("model").frame(width: 220, alignment: .leading)
                ForEach(setting.columns ?? ["input", "output", "cache read"], id: \.self) { Text($0).frame(width: 80) }
            }
            .font(.caption).foregroundStyle(.secondary)
            ForEach(rows.indices, id: \.self) { i in
                HStack {
                    TextField("provider/model-id", text: Binding(get: { rows[i].model }, set: { rows[i].model = $0 }))
                        .frame(width: 220).textFieldStyle(.roundedBorder).accessibilityLabel("Model")
                    ForEach(0..<3, id: \.self) { j in
                        TextField("", text: Binding(
                            get: { j < rows[i].prices.count ? rows[i].prices[j] : "" },
                            set: { new in
                                while rows[i].prices.count < 3 { rows[i].prices.append("") }
                                rows[i].prices[j] = new
                            }
                        ))
                        .frame(width: 80).textFieldStyle(.roundedBorder).multilineTextAlignment(.trailing)
                        .accessibilityLabel("Price \(j)")
                    }
                    Button { rows.remove(at: i) } label: { Image(systemName: "minus.circle") }.buttonStyle(.plain).accessibilityLabel("Remove model")
                }
            }
            Button { rows.append(("", ["", "", ""])) } label: { Label("Add a model", systemImage: "plus.circle") }.buttonStyle(.plain)
        }
        .onAppear { load() }
        .onChange(of: store.values[setting.key]) { _, _ in load() }
    }

    private func load() {
        let object = store.value(setting.key).objectValue ?? [:]
        rows = object.keys.sorted().map { key in
            (key, (object[key]?.arrayValue ?? []).map { JSONValue.number($0.doubleValue ?? 0).description })
        }
    }

    private func commit() {
        var object: [String: JSONValue] = [:]
        for row in rows where !row.model.isEmpty {
            let numbers = row.prices.compactMap { $0.isEmpty ? nil : Double($0) }
            if numbers.count >= 2 { object[row.model] = .array(numbers.map { .number($0) }) }
        }
        Task { await store.set(setting.key, .object(object)) }
    }
}

/// A whole pane drawn from the schema: the plain settings, then the advanced ones folded.
struct GenericPane: View {
    @EnvironmentObject private var store: SettingsStore
    let group: String
    let title: String
    let help: String
    @State private var showAdvanced = false

    var body: some View {
        let settings = store.schema?.settings(in: group) ?? []
        Form {
            Section {
                Text(help).font(.callout).foregroundStyle(.secondary)
            }
            Section {
                ForEach(settings.filter { !$0.advanced && $0.control != "folders" }) { SettingRow(setting: $0) }
            }
            let advanced = settings.filter { $0.advanced }
            if !advanced.isEmpty {
                Section {
                    DisclosureGroup("Advanced", isExpanded: $showAdvanced) {
                        ForEach(advanced) { SettingRow(setting: $0) }
                    }
                }
            }
            Section {
                HStack {
                    Spacer()
                    Button("Revert this pane to the defaults") { Task { await store.resetGroup(group) } }
                }
            }
        }
        .formStyle(.grouped)
        .navigationTitle(title)
    }
}
