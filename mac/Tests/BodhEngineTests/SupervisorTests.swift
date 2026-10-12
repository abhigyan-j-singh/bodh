import XCTest
@testable import BodhEngine

/// A scripted engine: announces a port (or not), can be made to crash, records the signals.
final class FakeProcess: EngineProcessHandle {
    let pid: Int32
    var running = true
    var lines: [String]
    var terminated = 0
    var killed = 0
    var stdinClosed = false
    var exitHandler: ((Int32) -> Void)?
    var isRunning: Bool { running }

    init(pid: Int32, lines: [String]) {
        self.pid = pid
        self.lines = lines
    }

    func readLine() async -> String? { lines.isEmpty ? nil : lines.removeFirst() }
    func closeStdin() { stdinClosed = true }
    func terminate() { terminated += 1; crash(code: 0) }
    func forceKill() { killed += 1; crash(code: 9) }
    func setExitHandler(_ handler: @escaping (Int32) -> Void) { exitHandler = handler }

    func crash(code: Int32) {
        guard running else { return }
        running = false
        exitHandler?(code)
    }
}

final class FakeLauncher: EngineLauncher {
    var launched: [FakeProcess] = []
    var announce = true
    var failToLaunch = false
    var nextPid: Int32 = 100

    func launch(executable: URL, arguments: [String], environment: [String: String], currentDirectory: URL?) throws -> EngineProcessHandle {
        if failToLaunch { throw NSError(domain: "test", code: 1, userInfo: [NSLocalizedDescriptionKey: "no such file"]) }
        nextPid += 1
        let line = announce ? "{\"port\": 50000, \"pid\": \(nextPid), \"started\": 1.0}" : "garbage"
        let process = FakeProcess(pid: nextPid, lines: [line])
        launched.append(process)
        return process
    }
}

final class FakeProbe: HealthProbe {
    var healthy = true
    func isHealthy(port: Int, token: String) async -> Bool { healthy }
}

final class SupervisorTests: XCTestCase {
    var clock = Date(timeIntervalSince1970: 1_000_000)
    var slept: [Double] = []
    var realDelay: Double = 0.01  // the fake sleep moves the clock and waits this long for real

    func make(launcher: FakeLauncher, probe: FakeProbe) -> Supervisor {
        var configuration = SupervisorConfiguration(
            executable: URL(fileURLWithPath: "/engine/python3"), arguments: ["-m", "bodh.cli", "serve", "--app"],
            environment: [:], token: "t"
        )
        configuration.readyTimeout = 10
        return Supervisor(
            configuration: configuration, launcher: launcher, probe: probe,
            sleep: { [weak self] seconds in
                self?.slept.append(seconds)
                self?.clock.addTimeInterval(seconds)
                try? await Task.sleep(nanoseconds: UInt64((self?.realDelay ?? 0) * 1_000_000_000))
            },
            now: { [weak self] in self?.clock ?? Date() }
        )
    }

    func testStartsAndBecomesReady() async {
        let launcher = FakeLauncher()
        let supervisor = make(launcher: launcher, probe: FakeProbe())
        await supervisor.start()
        let state = await supervisor.state
        XCTAssertEqual(state, .ready(port: 50000, pid: 101))
        XCTAssertEqual(launcher.launched.count, 1)
        XCTAssertEqual(launcher.launched[0].pid, 101)
    }

    func testNoPortLineIsDownWithBackoffThenRetries() async {
        let launcher = FakeLauncher()
        launcher.announce = false
        realDelay = 0.3  // room to flip the launcher before the restart runs
        let supervisor = make(launcher: launcher, probe: FakeProbe())
        await supervisor.start()
        let state = await supervisor.state
        guard case .down(let reason, let retry) = state else { return XCTFail("expected down, got \(state)") }
        XCTAssertTrue(reason.contains("announce"), reason)
        XCTAssertEqual(retry, 1)
        XCTAssertEqual(launcher.launched[0].terminated, 1)
        launcher.announce = true
        // the restart task sleeps through the fake clock and launches again
        for _ in 0..<50 {
            if await supervisor.state.isReady { break }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        let ready = await supervisor.state
        XCTAssertTrue(ready.isReady, "\(ready)")
        XCTAssertEqual(launcher.launched.count, 2)
    }

    func testCrashRestartsWithGrowingBackoffAndGivesUpAfterTenInAnHour() async {
        let launcher = FakeLauncher()
        let supervisor = make(launcher: launcher, probe: FakeProbe())
        await supervisor.start()
        var delays: [Double] = []
        for i in 0..<11 {
            let current = launcher.launched.last!
            current.crash(code: 1)
            for _ in 0..<50 {
                let state = await supervisor.state
                if case .down(_, let retry) = state { delays.append(retry); break }
                if case .gaveUp = state { break }
                try? await Task.sleep(nanoseconds: 10_000_000)
            }
            for _ in 0..<50 {
                let state = await supervisor.state
                if state.isReady { break }
                if case .gaveUp = state { break }
                try? await Task.sleep(nanoseconds: 10_000_000)
            }
            let state = await supervisor.state
            if case .gaveUp(let reason) = state {
                XCTAssertEqual(i, 10, "gave up after the eleventh failure in an hour")
                XCTAssertTrue(reason.contains("11 times"), reason)
                break
            }
        }
        XCTAssertEqual(Array(delays.prefix(5)), [1, 2, 4, 8, 30])
        let final = await supervisor.state
        if case .gaveUp = final {} else { XCTFail("expected to give up, got \(final)") }
        let restarts = await supervisor.restarts.count
        XCTAssertEqual(restarts, 11)
    }

    func testStopTerminatesThenKills() async {
        let launcher = FakeLauncher()
        let supervisor = make(launcher: launcher, probe: FakeProbe())
        await supervisor.start()
        let process = launcher.launched[0]
        await supervisor.stop()
        XCTAssertTrue(process.stdinClosed)
        XCTAssertEqual(process.terminated, 1)
        XCTAssertEqual(process.killed, 0)
        let state = await supervisor.state
        XCTAssertEqual(state, .stopped)
    }

    func testHealthTimeoutStopsTheProcess() async {
        let launcher = FakeLauncher()
        let probe = FakeProbe()
        probe.healthy = false
        let supervisor = make(launcher: launcher, probe: probe)
        await supervisor.start()
        let state = await supervisor.state
        guard case .down(let reason, _) = state else { return XCTFail("expected down, got \(state)") }
        XCTAssertTrue(reason.contains("did not answer"), reason)
        XCTAssertEqual(launcher.launched[0].terminated, 1)
        await supervisor.stop()
    }

    func testLaunchFailureIsReported() async {
        let launcher = FakeLauncher()
        launcher.failToLaunch = true
        let supervisor = make(launcher: launcher, probe: FakeProbe())
        await supervisor.start()
        let state = await supervisor.state
        guard case .down(let reason, _) = state else { return XCTFail("expected down, got \(state)") }
        XCTAssertTrue(reason.contains("could not be started"), reason)
        await supervisor.stop()
    }

    func testAnnouncementParses() {
        XCTAssertEqual(EngineAnnouncement.parse("{\"port\": 5, \"pid\": 6, \"started\": 1.5, \"url\": \"http://127.0.0.1:5\", \"token_file\": \"/t\"}")?.port, 5)
        XCTAssertNil(EngineAnnouncement.parse("serving: a run every 30 min"))
    }

    func testTokenFileIsPrivate() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let url = dir.appendingPathComponent("token")
        let token = try Supervisor.writeToken(to: url)
        XCTAssertEqual(token.count, 64)
        XCTAssertEqual(try String(contentsOf: url, encoding: .utf8), token)
        let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
        let mode = (attributes[.posixPermissions] as? Int) ?? 0
        XCTAssertEqual(mode & 0o777, 0o600)
        try FileManager.default.removeItem(at: dir)
    }
}
