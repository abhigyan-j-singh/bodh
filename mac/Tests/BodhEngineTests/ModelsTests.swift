import XCTest
@testable import BodhEngine

final class ModelsTests: XCTestCase {
    func testHealthDecodes() throws {
        let health = try Fixtures.decode(Health.self, "health.json")
        XCTAssertEqual(health.engine, "bodh")
        XCTAssertTrue(health.appMode)
        XCTAssertEqual(health.setupNeeded, ["reader", "key"])
        XCTAssertTrue(health.needsOnboarding)
        XCTAssertNil(health.lastRun)
    }

    func testStatusDecodes() throws {
        let status = try Fixtures.decode(Status.self, "status.json")
        XCTAssertEqual(status.mode, "scoring-only")
        XCTAssertFalse(status.isAutocut)
        XCTAssertEqual(status.pile.unread, 4)
        XCTAssertEqual(status.config.judge, "claude-sonnet-5-5")
        XCTAssertEqual(status.attention, [])
        XCTAssertNil(status.spend.cacheHit)
        XCTAssertNotNil(status.child)
    }

    func testListWhyFindTagsDecode() throws {
        let list = try Fixtures.decode(ListAnswer.self, "list.json")
        XCTAssertEqual(list.band, 70)
        XCTAssertEqual(list.articles.map { $0.score ?? 0 }, [92, 75])
        XCTAssertEqual(list.articles.first?.band, "score-90")
        let why = try Fixtures.decode(WhyAnswer.self, "why.json")
        XCTAssertTrue(why.article.judged)
        XCTAssertEqual(why.article.score, 92)
        XCTAssertEqual(why.article.record, [])  // the fixture is written before any mark
        let found = try Fixtures.decode(FindAnswer.self, "find.json")
        XCTAssertEqual(found.hits.count, 4)
        XCTAssertTrue(found.unread)
        let tags = try Fixtures.decode(TagsAnswer.self, "tags.json")
        XCTAssertTrue(tags.tags.contains { $0.tag == "great" && $0.writer == "you" })
    }

    func testSettingsSchemaAndValuesDecode() throws {
        let schema = try Fixtures.decode(SettingsSchema.self, "settings_schema.json")
        XCTAssertEqual(schema.groups.first?.key, "account")
        let cutoff = try XCTUnwrap(schema.settings.first { $0.key == "autocut.cutoff" })
        XCTAssertEqual(cutoff.control, "number")
        XCTAssertEqual(cutoff.min, 0)
        XCTAssertEqual(cutoff.max, 100)
        XCTAssertEqual(cutoff.value, .number(35))
        XCTAssertTrue(cutoff.isInteger)
        let folders = try XCTUnwrap(schema.settings.first { $0.key == "folders" })
        XCTAssertEqual(folders.fields?.map { $0.key }, ["protected", "cutoff", "judge"])
        XCTAssertEqual(schema.settings(in: "cutting").isEmpty, false)
        let values = try Fixtures.decode(SettingsAnswer.self, "settings.json")
        XCTAssertEqual(values.settings["autocut.cutoff"], .number(35))
        XCTAssertEqual(values.sources["prune.keep"], "default")
        XCTAssertEqual(values.settings["folders"]?["News"]?["cutoff"], .number(70))
    }

    func testFoldersKeysProfileAttentionDecode() throws {
        let folders = try Fixtures.decode(FoldersAnswer.self, "folders.json")
        XCTAssertEqual(folders.folders.first { $0.name == "News" }?.cutoff, 70)
        XCTAssertNil(folders.folders.first?.inAccount)
        let keys = try Fixtures.decode(KeysAnswer.self, "keys.json")
        XCTAssertEqual(keys.row("anthropic")?.set, true)
        XCTAssertEqual(keys.row("anthropic")?.variables.first?.source, "keychain")
        XCTAssertEqual(keys.keychainService, "bodh")
        let profile = try Fixtures.decode(ProfileAnswer.self, "profile.json")
        XCTAssertEqual(profile.questions.map { $0.key }, ["who", "knows", "goal", "decisions", "never"])
        XCTAssertTrue(profile.about.hasPrefix("# About the student"))
        let attention = try Fixtures.decode(AttentionAnswer.self, "attention.json")
        XCTAssertEqual(attention.attention.first?.key, "provider")
        XCTAssertEqual(attention.attention.first?.level, "critical")
    }

    func testChecksAndPlansDecode() throws {
        let reader = try Fixtures.decode(ReaderCheck.self, "check_reader.json")
        XCTAssertTrue(reader.ok)
        XCTAssertEqual(reader.folders, ["Essays", "News"])
        let provider = try Fixtures.decode(ProviderCheck.self, "check_provider.json")
        XCTAssertEqual(provider.model, "claude-sonnet-5-5")
        XCTAssertNotNil(provider.cents)
        let plan = try Fixtures.decode(PlanAnswer.self, "prune_plan.json")
        XCTAssertEqual(plan.prunePlan?.cut, 2)
        XCTAssertEqual(plan.prunePlan?.best.first?.score, 40)
        XCTAssertNil(plan.catchUpPlan)
        XCTAssertEqual(plan.button, "Cut 2")
    }

    func testJSONValueRoundTripsAndEncodesIntegersPlainly() throws {
        let value = JSONValue.object(["a": .number(35), "b": .number(0.25), "c": .bool(true), "d": .null, "e": .array([.string("x")])])
        let data = try JSONEncoder().encode(value)
        let text = String(decoding: data, as: UTF8.self)
        XCTAssertTrue(text.contains("\"a\":35"), text)
        XCTAssertTrue(text.contains("\"b\":0.25"), text)
        let back = try JSONDecoder().decode(JSONValue.self, from: data)
        XCTAssertEqual(back, value)
        XCTAssertEqual(JSONValue.number(35).description, "35")
        XCTAssertEqual(JSONValue.number(0.2).description, "0.2")
    }
}
