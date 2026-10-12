import AppKit
import SwiftUI
import BodhEngine

/// The sections of the window, in the reader's order; six are the engine's own pages, the
/// log is native.
enum WindowSection: String, CaseIterable, Identifiable {
    case today, list, verdicts, learning, feeds, money, log

    var id: String { rawValue }

    var title: String {
        switch self {
        case .today: return "Today"
        case .list: return "Reading list"
        case .verdicts: return "Your verdicts"
        case .learning: return "Learning"
        case .feeds: return "Feeds"
        case .money: return "Money and runs"
        case .log: return "Log"
        }
    }

    var symbol: String {
        switch self {
        case .today: return "sun.max"
        case .list: return "list.bullet.rectangle"
        case .verdicts: return "hand.thumbsup"
        case .learning: return "brain.head.profile"
        case .feeds: return "dot.radiowaves.left.and.right"
        case .money: return "chart.bar"
        case .log: return "text.alignleft"
        }
    }

    /// The engine's section name, or nil for the native views.
    var engineSection: String? { self == .log ? nil : rawValue }
}

@MainActor struct MainWindow: View {
    @EnvironmentObject private var model: AppModel
    @State private var section: WindowSection? = .today

    var body: some View {
        Group {
            if model.engineMissing {
                EngineMissingView()
            } else if let offer = model.migration {
                MigrationView(offer: offer)
            } else if !model.engineState.isReady {
                EngineStateView()
            } else if model.needsOnboarding {
                OnboardingView()
            } else {
                split
            }
        }
        .frame(minWidth: 860, minHeight: 560)
    }

    private var split: some View {
        NavigationSplitView {
            List(WindowSection.allCases, selection: $section) { item in
                Label(item.title, systemImage: item.symbol)
                    .tag(item)
            }
            .navigationSplitViewColumnWidth(min: 170, ideal: 190, max: 240)
        } detail: {
            VStack(spacing: 0) {
                if let worst = model.worstAttention {
                    AttentionBanner(item: worst)
                }
                if let notice = model.notice {
                    NoticeBar(text: notice)
                }
                detail(for: section ?? .today)
            }
        }
        .toolbar {
            ToolbarItemGroup(placement: .primaryAction) {
                Button {
                    model.runNow()
                } label: {
                    Label("Run now", systemImage: "play.fill")
                }
                .help("Judge what arrived since the last run")
                .disabled(model.isRunning || model.childRunning)
                Button {
                    model.stop()
                } label: {
                    Label("Stop", systemImage: "stop.fill")
                }
                .help("Stop the run in progress; what was judged stays")
                .disabled(!(model.isRunning || model.childRunning))
                Menu {
                    Button("For an hour") { model.pause(minutes: 60) }
                    Button("Until tomorrow morning") { model.pauseUntilTomorrow() }
                    Button("Until resumed") { model.pause(minutes: 60 * 24 * 3650) }
                    Divider()
                    Button("Resume") { model.resume() }.disabled(!model.isPaused)
                } label: {
                    Label(model.isPaused ? "Paused" : "Pause", systemImage: model.isPaused ? "pause.circle.fill" : "pause.fill")
                }
                .help("Hold the schedule for a while")
            }
            ToolbarItem(placement: .status) {
                Text(model.sentence)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .navigationTitle("bodh")
    }

    @ViewBuilder
    private func detail(for section: WindowSection) -> some View {
        if let name = section.engineSection, let client = model.client {
            EngineWebView(client: client, section: name, generation: model.engineState.port ?? 0)
                .id(name)
        } else {
            LogView()
        }
    }
}

/// The one banner: the most severe condition, its sentence, Dismiss when it may be waved away.
@MainActor struct AttentionBanner: View {
    @EnvironmentObject private var model: AppModel
    let item: Attention

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Image(systemName: item.level == "good" ? "checkmark.circle" : item.level == "critical" ? "exclamationmark.octagon" : "exclamationmark.triangle")
                .foregroundStyle(item.level == "good" ? Color.green : item.level == "critical" ? Color.red : Color.orange)
            VStack(alignment: .leading, spacing: 2) {
                Text(item.word).fontWeight(.semibold)
                Text(item.text).font(.callout).foregroundStyle(.secondary)
            }
            Spacer()
            if item.key == "signin" || item.key == "provider" {
                SettingsLink { Text("Open Settings") }
            }
            if item.dismissable {
                Button("Dismiss") { model.dismiss(item) }
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(.quaternary.opacity(0.4))
        .accessibilityElement(children: .combine)
    }
}

/// What the last action answered, for a few seconds.
@MainActor struct NoticeBar: View {
    @EnvironmentObject private var model: AppModel
    let text: String

    var body: some View {
        HStack {
            Text(text).font(.callout)
            Spacer()
            Button { model.notice = nil } label: { Image(systemName: "xmark") }
                .buttonStyle(.plain)
                .accessibilityLabel("Hide")
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 6)
        .background(.quaternary.opacity(0.25))
        .task(id: text) {
            try? await Task.sleep(nanoseconds: 12_000_000_000)
            if model.notice == text { model.notice = nil }
        }
    }
}

/// The engine is starting, down or given up: the sentence, the last lines it said, Retry.
@MainActor struct EngineStateView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: "book.closed")
                .font(.system(size: 44))
                .foregroundStyle(.secondary)
            Text(model.engineState.description)
                .font(.title3)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 520)
            switch model.engineState {
            case .launching:
                ProgressView()
            case .down, .gaveUp:
                HStack {
                    Button("Retry") { model.retryEngine() }.keyboardShortcut(.defaultAction)
                    Button("Show the log") { Reveal.dataFolder() }
                }
                EngineLogTail()
            default:
                EmptyView()
            }
        }
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// The last lines the engine printed, from the app's own log.
@MainActor struct EngineLogTail: View {
    var body: some View {
        let lines = AppLog.tail(400).filter { $0.contains(" ENGINE ") }.suffix(12)
        if !lines.isEmpty {
            ScrollView {
                Text(lines.map { String($0.dropFirst(27)) }.joined(separator: "\n"))
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxWidth: 620, maxHeight: 160)
            .padding(8)
            .background(.quaternary.opacity(0.3))
            .clipShape(RoundedRectangle(cornerRadius: 6))
        }
    }
}

@MainActor struct EngineMissingView: View {
    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle").font(.system(size: 40)).foregroundStyle(.orange)
            Text("The engine is missing from this copy of bodh.").font(.title3)
            Text("The app was built without Contents/Frameworks/engine. Build it with `make app` in mac/, or point BODH_ENGINE at an engine folder made by scripts/build_engine.sh while developing.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 520)
        }
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// The engine's log tail, refreshed every few seconds while the section is open.
@MainActor struct LogView: View {
    @EnvironmentObject private var model: AppModel
    @State private var lines: [String] = []

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    ForEach(Array(lines.enumerated()), id: \.offset) { index, line in
                        Text(line)
                            .font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled)
                            .id(index)
                    }
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .task {
                while !Task.isCancelled {
                    if let client = model.client, let fresh = try? await client.log(lines: 300) {
                        if fresh != lines {
                            lines = fresh
                            proxy.scrollTo(max(0, fresh.count - 1), anchor: .bottom)
                        }
                    }
                    try? await Task.sleep(nanoseconds: 4_000_000_000)
                }
            }
        }
    }
}
