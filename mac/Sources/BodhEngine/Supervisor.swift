import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
#if canImport(Darwin)
import Darwin
#elseif canImport(Glibc)
import Glibc
#endif

/// Where the engine is, as the menu bar shows it.
public enum SupervisorState: Equatable, CustomStringConvertible {
    case stopped
    case launching(attempt: Int)
    case ready(port: Int, pid: Int32)
    case down(reason: String, retryInSeconds: Double)
    case gaveUp(reason: String)

    public var description: String {
        switch self {
        case .stopped: return "The engine is stopped."
        case .launching(let n): return n <= 1 ? "Starting the engine…" : "Starting the engine again (attempt \(n))…"
        case .ready(let port, _): return "The engine is running on port \(port)."
        case .down(let reason, let retry): return "\(reason) Trying again in \(Int(retry.rounded())) s."
        case .gaveUp(let reason): return "\(reason) The engine stays stopped until you press Retry."
        }
    }

    public var isReady: Bool { if case .ready = self { return true } else { return false } }
    public var port: Int? { if case .ready(let port, _) = self { return port } else { return nil } }
}

/// The lines `bodh serve --app` prints first: the port, the pid, where the token file is.
public struct EngineAnnouncement: Codable, Equatable {
    public var port: Int
    public var pid: Int
    public var started: Double
    public var url: String?
    public var tokenFile: String?

    public static func parse(_ line: String) -> EngineAnnouncement? {
        guard let data = line.data(using: .utf8) else { return nil }
        return try? EngineClient.decoder.decode(EngineAnnouncement.self, from: data)
    }
}

/// A running engine process, as the supervisor drives it.
public protocol EngineProcessHandle: AnyObject {
    var pid: Int32 { get }
    var isRunning: Bool { get }
    /// The next line of stdout, or nil when the pipe closed.
    func readLine() async -> String?
    func closeStdin()
    func terminate()
    func forceKill()
    func setExitHandler(_ handler: @escaping (Int32) -> Void)
}

public protocol EngineLauncher {
    func launch(executable: URL, arguments: [String], environment: [String: String], currentDirectory: URL?) throws -> EngineProcessHandle
}

public protocol HealthProbe {
    func isHealthy(port: Int, token: String) async -> Bool
}

public struct SupervisorConfiguration {
    public var executable: URL
    public var arguments: [String]
    public var environment: [String: String]
    public var currentDirectory: URL?
    public var token: String
    public var readyTimeout: Double = 10
    public var probeInterval: Double = 0.25
    public var backoff: [Double] = [1, 2, 4, 8, 30]
    public var maxRestartsPerHour: Int = 10
    public var stopGrace: Double = 5

    public init(executable: URL, arguments: [String], environment: [String: String], currentDirectory: URL? = nil, token: String) {
        self.executable = executable
        self.arguments = arguments
        self.environment = environment
        self.currentDirectory = currentDirectory
        self.token = token
    }
}

/// Starts the engine, waits for its port line and a healthy `/api/health`, restarts it after a
/// crash with backoff (1, 2, 4, 8, 30 s; ten in an hour and it gives up), and stops it on quit
/// (SIGTERM, a grace period, SIGKILL). One engine at a time. The clock and the sleep are
/// injectable so the state machine is tested without waiting.
public actor Supervisor {
    public private(set) var state: SupervisorState = .stopped
    public private(set) var lastStderr: [String] = []
    public private(set) var restarts: [Date] = []

    private let configuration: SupervisorConfiguration
    private let launcher: EngineLauncher
    private let probe: HealthProbe
    private let sleep: (Double) async -> Void
    private let now: () -> Date
    private var process: EngineProcessHandle?
    private var stopping = false
    private var attempt = 0
    private var observers: [UUID: (SupervisorState) -> Void] = [:]
    private var restartTask: Task<Void, Never>?

    public init(
        configuration: SupervisorConfiguration,
        launcher: EngineLauncher,
        probe: HealthProbe,
        sleep: @escaping (Double) async -> Void = { try? await Task.sleep(nanoseconds: UInt64($0 * 1_000_000_000)) },
        now: @escaping () -> Date = Date.init
    ) {
        self.configuration = configuration
        self.launcher = launcher
        self.probe = probe
        self.sleep = sleep
        self.now = now
    }

    /// Called with every change of state, on the actor; the app forwards it to the main thread.
    @discardableResult
    public func observe(_ handler: @escaping (SupervisorState) -> Void) -> UUID {
        let id = UUID()
        observers[id] = handler
        handler(state)
        return id
    }

    public func stopObserving(_ id: UUID) { observers[id] = nil }

    private func set(_ new: SupervisorState) {
        state = new
        for handler in observers.values { handler(new) }
    }

    public func noteStderr(_ line: String) {
        lastStderr.append(line)
        if lastStderr.count > 200 { lastStderr.removeFirst(lastStderr.count - 200) }
    }

    // MARK: start

    /// Start the engine (or do nothing when it already runs). Returns when it is ready, down or given up.
    public func start() async {
        if case .ready = state, process?.isRunning == true { return }
        stopping = false
        restartTask?.cancel()
        restartTask = nil
        attempt += 1
        await launchOnce()
    }

    /// After a crash: start again, from the first backoff step.
    public func retry() async {
        attempt = 0
        restarts = []
        await start()
    }

    private func launchOnce() async {
        set(.launching(attempt: attempt))
        let handle: EngineProcessHandle
        do {
            handle = try launcher.launch(
                executable: configuration.executable,
                arguments: configuration.arguments,
                environment: configuration.environment,
                currentDirectory: configuration.currentDirectory
            )
        } catch {
            await failed("The engine could not be started: \(error.localizedDescription)")
            return
        }
        process = handle
        handle.setExitHandler { [weak self] code in
            guard let self = self else { return }
            Task { await self.exited(code: code, handle: handle) }
        }
        guard let line = await handle.readLine(), let announcement = EngineAnnouncement.parse(line) else {
            guard process === handle else { return }  // it exited meanwhile; that path reported it
            abandon(handle)
            await failed("The engine did not announce its port.")
            return
        }
        let deadline = now().addingTimeInterval(configuration.readyTimeout)
        while now() < deadline {
            guard process === handle else { return }  // exited meanwhile, reported by `exited`
            if await probe.isHealthy(port: announcement.port, token: configuration.token) {
                guard process === handle else { return }
                set(.ready(port: announcement.port, pid: Int32(announcement.pid)))
                return
            }
            await sleep(configuration.probeInterval)
        }
        guard process === handle else { return }
        abandon(handle)
        await failed("The engine started but did not answer within \(Int(configuration.readyTimeout)) seconds.")
    }

    /// Forget a process before ending it, so its exit is not reported a second time.
    private func abandon(_ handle: EngineProcessHandle) {
        process = nil
        handle.closeStdin()
        handle.terminate()
    }

    private func exited(code: Int32, handle: EngineProcessHandle) async {
        guard process === handle else { return }  // an old or abandoned process
        process = nil
        if stopping {
            set(.stopped)
            return
        }
        await failed("The engine stopped (exit \(code)).")
    }

    private func failed(_ reason: String) async {
        process = nil
        let hourAgo = now().addingTimeInterval(-3600)
        restarts = restarts.filter { $0 > hourAgo } + [now()]
        if restarts.count > configuration.maxRestartsPerHour {
            set(.gaveUp(reason: reason + " It failed \(restarts.count) times in an hour."))
            return
        }
        let delay = configuration.backoff[min(max(attempt - 1, 0), configuration.backoff.count - 1)]
        set(.down(reason: reason, retryInSeconds: delay))
        restartTask = Task { [weak self] in
            guard let self = self else { return }
            await self.sleep(delay)
            guard !Task.isCancelled else { return }
            await self.restartAfterBackoff()
        }
    }

    private func restartAfterBackoff() async {
        guard !stopping, process == nil else { return }
        attempt += 1
        await launchOnce()
    }

    // MARK: stop

    /// SIGTERM (and the end of stdin), the grace period, then SIGKILL.
    public func stop() async {
        stopping = true
        restartTask?.cancel()
        restartTask = nil
        guard let handle = process else {
            set(.stopped)
            return
        }
        handle.closeStdin()
        handle.terminate()
        let deadline = now().addingTimeInterval(configuration.stopGrace)
        while handle.isRunning && now() < deadline {
            await sleep(0.1)
        }
        if handle.isRunning {
            handle.forceKill()
        }
        process = nil
        set(.stopped)
    }

    // MARK: the token file

    /// A fresh token, written to `url` readable by its owner alone, before the engine starts.
    public static func writeToken(to url: URL) throws -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        for i in 0..<bytes.count { bytes[i] = UInt8.random(in: 0...255) }
        let token = bytes.map { String(format: "%02x", $0) }.joined()
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try token.write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        return token
    }
}

// MARK: the real process

/// `Process` with pipes: stdin held open for the engine's life (its end is the app's end),
/// stdout read line by line for the announcement, stderr kept as the last lines for the log.
public final class ProcessEngineHandle: EngineProcessHandle {
    private let process = Process()
    private let stdin = Pipe()
    private let stdout = Pipe()
    private let stderr = Pipe()
    private let lock = NSLock()
    private var buffer = Data()
    private var lines: [String] = []
    private var waiters: [CheckedContinuation<String?, Never>] = []
    private var closed = false
    private var exitHandler: ((Int32) -> Void)?
    public var onStderr: ((String) -> Void)?

    public init(executable: URL, arguments: [String], environment: [String: String], currentDirectory: URL?) throws {
        process.executableURL = executable
        process.arguments = arguments
        process.environment = environment
        if let dir = currentDirectory { process.currentDirectoryURL = dir }
        process.standardInput = stdin
        process.standardOutput = stdout
        process.standardError = stderr
        stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
            self?.consume(handle.availableData)
        }
        stderr.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            for line in String(decoding: data, as: UTF8.self).split(separator: "\n") {
                self?.onStderr?(String(line))
            }
        }
        process.terminationHandler = { [weak self] proc in
            self?.stdout.fileHandleForReading.readabilityHandler = nil
            self?.stderr.fileHandleForReading.readabilityHandler = nil
            self?.consume(Data())
            self?.exitHandler?(proc.terminationStatus)
        }
        try process.run()
    }

    private func consume(_ data: Data) {
        lock.lock()
        if data.isEmpty {
            closed = true
        } else {
            buffer.append(data)
            while let newline = buffer.firstIndex(of: 0x0A) {
                let line = String(decoding: buffer[buffer.startIndex..<newline], as: UTF8.self)
                buffer.removeSubrange(buffer.startIndex...newline)
                lines.append(line)
            }
        }
        var served: [(CheckedContinuation<String?, Never>, String?)] = []
        while let waiter = waiters.first {
            if let line = lines.first {
                lines.removeFirst()
                served.append((waiter, line))
            } else if closed {
                served.append((waiter, nil))
            } else {
                break
            }
            waiters.removeFirst()
        }
        lock.unlock()
        for (waiter, line) in served { waiter.resume(returning: line) }
    }

    public var pid: Int32 { process.processIdentifier }
    public var isRunning: Bool { process.isRunning }

    public func readLine() async -> String? {
        await withCheckedContinuation { (continuation: CheckedContinuation<String?, Never>) in
            lock.lock()
            if !lines.isEmpty {
                let line = lines.removeFirst()
                lock.unlock()
                continuation.resume(returning: line)
            } else if closed {
                lock.unlock()
                continuation.resume(returning: nil)
            } else {
                waiters.append(continuation)
                lock.unlock()
            }
        }
    }

    public func closeStdin() {
        try? stdin.fileHandleForWriting.close()
    }

    public func terminate() {
        if process.isRunning { process.terminate() }
    }

    public func forceKill() {
        if process.isRunning { _ = kill(process.processIdentifier, SIGKILL) }
    }

    public func setExitHandler(_ handler: @escaping (Int32) -> Void) {
        exitHandler = handler
    }
}

public struct ProcessLauncher: EngineLauncher {
    public var onStderr: ((String) -> Void)?

    public init(onStderr: ((String) -> Void)? = nil) {
        self.onStderr = onStderr
    }

    public func launch(executable: URL, arguments: [String], environment: [String: String], currentDirectory: URL?) throws -> EngineProcessHandle {
        let handle = try ProcessEngineHandle(executable: executable, arguments: arguments, environment: environment, currentDirectory: currentDirectory)
        handle.onStderr = onStderr
        return handle
    }
}

/// `/api/health` with the token, one short request.
public struct HTTPHealthProbe: HealthProbe {
    public init() {}

    public func isHealthy(port: Int, token: String) async -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/health") else { return false }
        var request = URLRequest(url: url)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 2
        guard let (_, response) = try? await URLSession.shared.data(for: request) else { return false }
        return (response as? HTTPURLResponse)?.statusCode == 200
    }
}
