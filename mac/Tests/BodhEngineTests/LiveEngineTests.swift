import Foundation
import XCTest
@testable import BodhEngine

/// The client and the supervisor against the real engine: `bodh serve --app` started as a
/// process from a Python that has bodh installed (`BODH_PYTHON`, the bundled engine's
/// interpreter on a Mac, a venv's elsewhere), on a scratch project. Skipped without
/// `BODH_LIVE_ENGINE=1`. This is the cross-boundary test: the Swift side's requests, the
/// engine's answers, the event stream, the token, the stop.
final class LiveEngineTests: XCTestCase {
    var root: URL!
    var supervisor: Supervisor!
    var client: EngineClient!

    override func setUp() async throws {
        try XCTSkipUnless(ProcessInfo.processInfo.environment["BODH_LIVE_ENGINE"] == "1", "set BODH_LIVE_ENGINE=1 and BODH_PYTHON")
        let python = ProcessInfo.processInfo.environment["BODH_PYTHON"] ?? "python3"
        root = FileManager.default.temporaryDirectory.appendingPathComponent("bodh-live-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        // the project files, the way the app makes them before its first start
        let skeleton = Process()
        skeleton.executableURL = URL(fileURLWithPath: python)
        skeleton.arguments = ["-m", "bodh.cli", "init", root.path, "--skeleton", "--timezone", "UTC"]
        skeleton.environment = ProcessInfo.processInfo.environment.merging(["PYTHONDONTWRITEBYTECODE": "1"]) { $1 }
        try skeleton.run()
        skeleton.waitUntilExit()
        XCTAssertEqual(skeleton.terminationStatus, 0)
        let tokenFile = root.appendingPathComponent("token")
        let token = try Supervisor.writeToken(to: tokenFile)
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        environment["BODH_HOME"] = root.path
        var configuration = SupervisorConfiguration(
            executable: URL(fileURLWithPath: python),
            arguments: ["-m", "bodh.cli", "serve", "--app", "--port", "0", "--token-file", tokenFile.path, "--home", root.path],
            environment: environment,
            currentDirectory: root,
            token: token
        )
        configuration.readyTimeout = 30
        supervisor = Supervisor(configuration: configuration, launcher: ProcessLauncher(), probe: HTTPHealthProbe())
        await supervisor.start()
        let state = await supervisor.state
        guard case .ready(let port, _) = state else {
            XCTFail("the engine did not come up: \(state)")
            return
        }
        client = EngineClient(baseURL: URL(string: "http://127.0.0.1:\(port)")!, token: token)
    }

    override func tearDown() async throws {
        if let supervisor = supervisor {
            await supervisor.stop()
            let state = await supervisor.state
            XCTAssertEqual(state, .stopped)
        }
        if let root = root { try? FileManager.default.removeItem(at: root) }
    }

    func testHealthStatusAndSettingsRoundTrip() async throws {
        let health = try await client.health()
        XCTAssertTrue(health.appMode)
        XCTAssertEqual(health.root, root.path)
        XCTAssertTrue(health.setupNeeded.contains("reader"))
        XCTAssertTrue(health.aboutIsExample)
        let status = try await client.status()
        XCTAssertEqual(status.mode, "scoring-only")
        XCTAssertNotNil(status.serve)
        XCTAssertEqual(status.pile.unread, 0)
        let schema = try await client.settingsSchema()
        XCTAssertTrue(schema.settings.contains { $0.key == "autocut.cutoff" })
        let before = try await client.settings()
        XCTAssertEqual(before.settings["autocut.cutoff"], .number(35))
        let after = try await client.update(settings: ["autocut.cutoff": .number(40), "prune.keep": .number(25)])
        XCTAssertEqual(after.changed, ["autocut.cutoff", "prune.keep"])
        XCTAssertEqual(after.settings["autocut.cutoff"], .number(40))
        XCTAssertEqual(after.sources["prune.keep"], "config.yaml")
        let text = try String(contentsOf: root.appendingPathComponent("config.yaml"), encoding: .utf8)
        XCTAssertTrue(text.contains("cutoff: 40"), text)
        XCTAssertTrue(text.contains("# the cut-off for folders without their own"), "the comments survive")
        do {
            _ = try await client.update(settings: ["autocut.cutoff": .number(140)])
            XCTFail("140 should be refused")
        } catch let error as EngineError {
            XCTAssertEqual(error.fieldErrors["autocut.cutoff"], "Input should be less than or equal to 100")
        }
        let list = try await client.list(band: 70)
        XCTAssertEqual(list.articles, [])
        let found = try await client.find("anything")
        XCTAssertEqual(found.hits, [])
        let tags = try await client.tags()
        XCTAssertTrue(tags.contains { $0.tag == "score-90" })
        let folders = try await client.folders()
        XCTAssertEqual(folders.defaultCutoff, 40)
        let keys = try await client.keys()
        XCTAssertEqual(keys.row("bazqux")?.set, false)
        XCTAssertEqual(keys.keychainService, "bodh")
        let profile = try await client.profile()
        XCTAssertTrue(profile.aboutIsExample)
        XCTAssertEqual(profile.questions.count, 5)
        let written = try await client.updateProfile(answers: ["who": "A", "knows": "B", "goal": "C", "decisions": "D", "never": "E"])
        XCTAssertFalse(written.aboutIsExample)
        XCTAssertEqual(written.written, ["about"])
        let attention = try await client.attention()
        XCTAssertEqual(attention, [])
        let paused = try await client.pause(minutes: 30)
        XCTAssertNotNil(paused.pausedUntil)
        let resumed = try await client.resume()
        XCTAssertNil(resumed.pausedUntil)
        // a refusal the engine composes, as a sentence
        do {
            _ = try await client.mark("nope", "great")
        } catch let error as EngineError {
            XCTAssertTrue(error.description.contains("not an article id"), error.description)
        }
        // the token is required
        let stranger = EngineClient(baseURL: client.baseURL, token: "wrong")
        do {
            _ = try await stranger.health()
            XCTFail("a wrong token must be refused")
        } catch let error as EngineError {
            if case .http(let status, _) = error { XCTAssertEqual(status, 401) } else { XCTFail("\(error)") }
        }
    }

    func testEventStreamCarriesTicksAndNamedEvents() async throws {
        let stream = client.events()
        var ticks = 0
        var sawSettings = false
        let deadline = Date().addingTimeInterval(20)
        // a change while the stream is open: the named event must arrive beside the ticks
        Task {
            try? await Task.sleep(nanoseconds: 500_000_000)
            _ = try? await self.client.update(settings: ["prune.keep": .number(30)])
        }
        for try await event in stream {
            if event.name == "tick" {
                ticks += 1
                let tick = try event.decode(Tick.self)
                XCTAssertFalse(tick.state.sentence.isEmpty)
            } else if event.name == "settings" {
                sawSettings = true
                XCTAssertNotNil(event.id)
                XCTAssertEqual(event.data["changed"], .array([.string("prune.keep")]))
            }
            if (ticks >= 1 && sawSettings) || Date() > deadline { break }
        }
        XCTAssertGreaterThanOrEqual(ticks, 1)
        XCTAssertTrue(sawSettings, "the settings event did not arrive within 20 s")
    }
}
