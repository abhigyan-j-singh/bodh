import AppKit
import Foundation
import SwiftUI
import BodhEngine

/// The one object every view reads: the engine's state, the live tick, the status, the
/// attention, and the levers. It owns the supervisor and the event stream; everything it
/// knows about reading comes from the engine.
@MainActor
final class AppModel: ObservableObject {
    @Published var engineState: SupervisorState = .stopped
    @Published var health: Health?
    @Published var status: Status?
    @Published var tick: TickState?
    @Published var attention: [Attention] = []
    @Published var generation = 0
    @Published var notice: String?  // the last sentence an action answered with
    @Published var engineMissing = false
    @Published var migration: MigrationOffer?
    @Published var onboardingDismissed = UserDefaults.standard.bool(forKey: "onboardingDismissed")
    @Published var connected = false  // the event stream is open

    private(set) var client: EngineClient?
    private var supervisor: Supervisor?
    private var eventsTask: Task<Void, Never>?
    private var refreshTask: Task<Void, Never>?
    private var lastEventID: Int?
    private var starting = false
    private var migrationDecided = false
    let notifications = Notifications()
    let loginItem = LoginItem()

    // MARK: what the views read

    var unread: Int? { status?.pile.unread }
    var isRunning: Bool { tick?.running ?? (status?.running != nil) }
    var isPaused: Bool { status?.pausedUntil != nil }
    var childRunning: Bool { status?.child?.running ?? false }

    var needsOnboarding: Bool {
        guard let health = health else { return false }
        return health.needsOnboarding && !onboardingDismissed
    }

    /// One sentence for the menu's first line and the window's toolbar.
    var sentence: String {
        if engineMissing { return "The engine is missing from the app." }
        switch engineState {
        case .ready:
            if let tick = tick { return tick.sentence }
            return "Connecting to the engine…"
        default:
            return engineState.description
        }
    }

    var nextRunText: String? {
        if isPaused, let until = status?.pausedUntil { return "Paused until " + TimeText.short(until) }
        guard let seconds = tick?.nextRunInS ?? status?.nextRunIn else { return nil }
        return "Next run in " + TimeText.minutes(seconds)
    }

    var worstAttention: Attention? {
        attention.first { $0.level == "critical" } ?? attention.first { $0.level == "warning" } ?? attention.first
    }

    var menuBarSymbol: String {
        if engineMissing { return "book.closed.fill" }
        switch engineState {
        case .ready:
            if let worst = worstAttention, worst.level != "good" { return "book.closed.circle.fill" }
            if isRunning || childRunning { return "book.fill" }
            if isPaused { return "book.closed" }
            return "book.closed"
        case .launching: return "book.closed"
        case .down, .gaveUp, .stopped: return "book.closed"
        }
    }

    // MARK: start and stop

    func start() {
        guard !starting else { return }
        starting = true
        Task { await self.startEngine() }
    }

    private func startEngine() async {
        defer { starting = false }
        do {
            try Paths.ensureDirectories()
        } catch {
            AppLog.error("cannot create Application Support/bodh: \(error.localizedDescription)")
        }
        guard let python = Paths.enginePython else {
            engineMissing = true
            AppLog.error("no engine: neither Contents/Frameworks/engine nor BODH_ENGINE")
            return
        }
        engineMissing = false
        if migration == nil, !migrationDecided, let offer = MigrationOffer.detect() {
            migration = offer
            migrationDecided = true
            AppLog.info("found a CLI install at \(offer.source.path): offering to move it")
            return  // the window asks; Continue or Not now starts the engine
        }
        if let result = EngineCLI.ensureProject(at: Paths.home), result.code != 0 {
            engineState = .gaveUp(reason: "The project files could not be written: \(result.output)")
            return
        }
        let token: String
        do {
            token = try Supervisor.writeToken(to: Paths.tokenFile)
        } catch {
            engineState = .gaveUp(reason: "The token file could not be written: \(error.localizedDescription)")
            return
        }
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        environment["BODH_HOME"] = Paths.home.path
        let configuration = SupervisorConfiguration(
            executable: python,
            arguments: ["-m", "bodh.cli", "serve", "--app", "--port", "0", "--token-file", Paths.tokenFile.path, "--home", Paths.home.path],
            environment: environment,
            currentDirectory: Paths.home,
            token: token
        )
        let supervisor = Supervisor(
            configuration: configuration,
            launcher: ProcessLauncher(onStderr: { line in AppLog.engine(line) }),
            probe: HTTPHealthProbe()
        )
        self.supervisor = supervisor
        await supervisor.observe { [weak self] state in
            Task { @MainActor in self?.engineStateChanged(state, token: token) }
        }
        await supervisor.start()
    }

    private func engineStateChanged(_ state: SupervisorState, token: String) {
        let wasReady = engineState.isReady
        engineState = state
        switch state {
        case .ready(let port, _):
            if !wasReady || client == nil {
                client = EngineClient(baseURL: URL(string: "http://127.0.0.1:\(port)")!, token: token)
                AppLog.info("engine ready on port \(port)")
                connect()
                Task { await self.finishMigrationIfPending() }
            }
        case .down(let reason, _), .gaveUp(let reason):
            AppLog.warning("engine down: \(reason)")
            disconnect()
        case .stopped:
            disconnect()
        case .launching:
            break
        }
    }

    /// The .env import the migration asked for, once the engine answers.
    private func finishMigrationIfPending() async {
        guard UserDefaults.standard.bool(forKey: "migration.importEnvPending"), let client = client else { return }
        var choices = Migration.Choices()
        choices.importEnv = true
        if let line = await Migration.finish(client: client, choices: choices) {
            AppLog.info("migration: \(line)")
            notice = line
        }
        UserDefaults.standard.removeObject(forKey: "migration.importEnvPending")
    }

    private func connect() {
        guard let client = client else { return }
        eventsTask?.cancel()
        refreshTask?.cancel()
        eventsTask = Task { await self.streamEvents(client) }
        refreshTask = Task { await self.refreshLoop(client) }
        Task { await self.refresh() }
    }

    private func disconnect() {
        eventsTask?.cancel()
        refreshTask?.cancel()
        eventsTask = nil
        refreshTask = nil
        connected = false
        tick = nil
    }

    /// The whole app ends with the engine: SIGTERM, the grace period, SIGKILL.
    func shutdown() async {
        disconnect()
        await supervisor?.stop()
        AppLog.info("engine stopped; quitting")
    }

    func retryEngine() {
        Task {
            if let supervisor = supervisor {
                await supervisor.retry()
            } else {
                await startEngine()
            }
        }
    }

    func restartEngine() {
        Task {
            await supervisor?.stop()
            await supervisor?.retry()
        }
    }

    // MARK: the stream and the refreshes

    private func streamEvents(_ client: EngineClient) async {
        while !Task.isCancelled {
            do {
                connected = true
                for try await event in client.events(since: lastEventID) {
                    if let id = event.id { lastEventID = id }
                    handle(event)
                }
            } catch {
                if Task.isCancelled { break }
                AppLog.warning("event stream ended: \(error.localizedDescription)")
            }
            connected = false
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
    }

    private func handle(_ event: EngineEvent) {
        switch event.name {
        case "tick":
            guard let tick = try? event.decode(Tick.self) else { return }
            self.tick = tick.state
            if tick.generation != generation {
                let first = generation == 0
                generation = tick.generation
                if !first { Task { await self.refreshStatus() } }
            }
        case "run":
            guard let run = try? event.decode(RunEvent.self) else { return }
            if run.state == "finished" {
                Task { await self.refresh() }
                if run.ok == false {
                    notifications.send(id: "run-\(run.runId ?? 0)", title: "The run failed", body: run.error ?? "See the log in the window.", kind: .runFailed)
                }
            }
        case "attention":
            if let items = event.data["attention"], let list = try? JSONEncoder().encode(items),
               let decoded = try? EngineClient.decoder.decode([Attention].self, from: list) {
                apply(attention: decoded)
            }
        case "settings":
            Task { await self.refresh() }
        default:
            break
        }
    }

    private func refreshLoop(_ client: EngineClient) async {
        while !Task.isCancelled {
            try? await Task.sleep(nanoseconds: 60_000_000_000)
            if Task.isCancelled { break }
            await refreshStatus()
        }
    }

    func refresh() async {
        guard let client = client else { return }
        do {
            health = try await client.health()
        } catch {
            AppLog.warning("health: \(error.localizedDescription)")
        }
        await refreshStatus()
    }

    func refreshStatus() async {
        guard let client = client else { return }
        do {
            let status = try await client.status()
            self.status = status
            apply(attention: status.attention)
        } catch {
            AppLog.warning("status: \(error.localizedDescription)")
        }
    }

    private func apply(attention items: [Attention]) {
        let seen = Set(attention.map { $0.id })
        attention = items
        for item in items where !seen.contains(item.id) {
            notifications.send(id: item.id, title: item.word, body: item.text, kind: .attention(item.key))
        }
    }

    // MARK: the levers

    private func act(_ what: String, _ call: @escaping () async throws -> ActionAnswer) {
        Task {
            do {
                let answer = try await call()
                notice = answer.message ?? (answer.ok ? "\(what): done" : "\(what): refused")
                await refreshStatus()
            } catch {
                notice = "\(what): \(error)"
                AppLog.warning("\(what): \(error)")
            }
        }
    }

    func runNow(mode: String = "normal") {
        guard let client = client else { return }
        act("Run now") { try await client.run(mode: mode) }
    }

    func stop() {
        guard let client = client else { return }
        act("Stop") { try await client.stop() }
    }

    func pause(minutes: Double) {
        guard let client = client else { return }
        act("Pause") { try await client.pause(minutes: minutes) }
    }

    func pauseUntilTomorrow() {
        guard let client = client else { return }
        act("Pause") { try await client.pauseUntilTomorrow() }
    }

    func resume() {
        guard let client = client else { return }
        act("Resume") { try await client.resume() }
    }

    func dismiss(_ item: Attention) {
        guard let client = client else { return }
        Task {
            if let list = try? await client.dismiss(attention: item.key) { attention = list }
        }
    }

    func openTodayInBazQux() {
        NSWorkspace.shared.open(URL(string: "https://bazqux.com/i/")!)
    }

    func finishOnboarding() {
        onboardingDismissed = true
        UserDefaults.standard.set(true, forKey: "onboardingDismissed")
        Task { await refresh() }
    }

    func resetOnboarding() {
        onboardingDismissed = false
        UserDefaults.standard.set(false, forKey: "onboardingDismissed")
    }
}

/// Short times for the menu: "7 min", "1 h 20 min", "Tue 07:00".
enum TimeText {
    static func minutes(_ seconds: Double) -> String {
        let total = Int(seconds.rounded())
        if total < 60 { return "under a minute" }
        let minutes = total / 60
        if minutes < 60 { return "\(minutes) min" }
        let hours = minutes / 60
        return hours < 48 ? "\(hours) h \(minutes % 60) min" : "\(hours / 24) days"
    }

    static func parse(_ iso: String) -> Date? {
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = parser.date(from: iso) { return date }
        parser.formatOptions = [.withInternetDateTime]
        return parser.date(from: iso)
    }

    static func short(_ iso: String) -> String {
        guard let date = parse(iso) else { return iso }
        let formatter = DateFormatter()
        formatter.dateFormat = Calendar.current.isDateInToday(date) ? "HH:mm" : "EEE HH:mm"
        return formatter.string(from: date)
    }
}
