import Foundation

// The engine's answers, as docs/json.md describes them. Keys are decoded from snake_case
// (`EngineClient.decoder`), so `next_run_in_s` is `nextRunInS` here. Anything the app does
// not need to understand stays a `JSONValue`. Every struct is Codable so the fixtures under
// tests/fixtures/api (written by the engine's own suite) decode in BodhEngineTests.

public struct Health: Codable, Equatable {
    public var schema: Int
    public var engine: String
    public var version: String
    public var pid: Int
    public var port: Int
    public var appMode: Bool
    public var uptimeS: Double
    public var schemaVersion: Int
    public var root: String
    public var dataDir: String
    public var configOk: Bool
    public var aboutIsExample: Bool
    public var readerConfigured: Bool
    public var keysMissing: [String]
    public var mode: String
    public var running: JSONValue?
    public var lastRun: LastRun?
    public var nextRunInS: Double?
    public var pausedUntil: String?
    public var setupNeeded: [String]

    public struct LastRun: Codable, Equatable {
        public var id: Int
        public var kind: String
        public var ok: Bool
        public var finishedAt: String?
        public var summary: String?
    }

    /// Onboarding is needed while the engine cannot run for anyone yet.
    public var needsOnboarding: Bool { !setupNeeded.isEmpty }
}

public struct Attention: Codable, Equatable, Identifiable {
    public var key: String
    public var level: String  // warning, critical, good
    public var word: String
    public var text: String
    public var linkText: String?
    public var linkUrl: String?
    public var dismissable: Bool
    public var fingerprint: String

    public var id: String { key + ":" + fingerprint }
}

public struct RunningProcess: Codable, Equatable {
    public var pid: Int
    public var kind: String
    public var started: Double
    public var elapsed: Double?
    public var phase: String?
}

public struct RunRow: Codable, Equatable, Identifiable {
    public var id: Int
    public var kind: String
    public var startedAt: String?
    public var finishedAt: String?
    public var ok: Bool?
    public var summary: String?
    public var error: String?
    public var spendUsd: Double?

    // the row carries more columns; only these are read
    enum CodingKeys: String, CodingKey { case id, kind, startedAt, finishedAt, ok, summary, error, spendUsd }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        kind = try c.decode(String.self, forKey: .kind)
        startedAt = try c.decodeIfPresent(String.self, forKey: .startedAt)
        finishedAt = try c.decodeIfPresent(String.self, forKey: .finishedAt)
        if let b = try? c.decodeIfPresent(Bool.self, forKey: .ok) {
            ok = b
        } else if let n = try? c.decodeIfPresent(Int.self, forKey: .ok) {
            ok = n != 0
        } else {
            ok = nil
        }
        summary = try c.decodeIfPresent(String.self, forKey: .summary)
        error = try c.decodeIfPresent(String.self, forKey: .error)
        spendUsd = try c.decodeIfPresent(Double.self, forKey: .spendUsd)
    }
}

public struct Status: Codable, Equatable {
    public struct Pile: Codable, Equatable {
        public var unread: Int
        public var judged: Int
        public var pending: Int
        public var inflight: Int
        public var tooOld: Int
        public var standIn: Int?
        public var rejudge: Int?
        public var lowConf: Int
    }

    public struct Spend: Codable, Equatable {
        public var today: Double
        public var week: Double
        public var all: Double
        public var per1000: Double?  // `per_1000` in the JSON: the snake-case strategy joins the digits on
        public var cacheHit: Double?
    }

    public struct Today: Codable, Equatable {
        public var arrived: Int
        public var judged: Int
        public var dupes: Int
        public var secondLooks: Int
        public var cut: Int
        public var spend: Double
    }

    public struct Child: Codable, Equatable {
        public var running: Bool
        public var args: [String]
        public var started: Double
        public var elapsed: Double
        public var code: Int?
        public var output: [String]
    }

    public struct Serve: Codable, Equatable {
        public var pid: Int
        public var everySeconds: Double
        public var started: Double
    }

    public struct ConfigSummary: Codable, Equatable {
        public var judge: String
        public var reviewer: String
        public var profiler: String
        public var budget: Double
        public var keep: Int
        public var cutoff: Int
        public var maxAgeDays: Int?
        public var root: String
        public var heartbeat: Bool
    }

    public var now: String
    public var mode: String
    public var safetyTripped: JSONValue?
    public var batchMode: Bool
    public var running: RunningProcess?
    public var serve: Serve?
    public var pausedUntil: String?
    public var nextRunIn: Double?
    public var attention: [Attention]
    public var config: ConfigSummary
    public var pile: Pile
    public var spend: Spend
    public var today: Today
    public var ratePerMin: Double?
    public var etaMin: Double?
    public var runs: [RunRow]
    public var lastOk: RunRow?
    public var lastError: RunRow?
    public var log: [String]
    public var child: Child?

    public var isAutocut: Bool { mode == "autocut" }
    public var isPaused: Bool { pausedUntil != nil }
}

public struct Article: Codable, Equatable, Identifiable {
    public var id: String
    public var title: String
    public var url: String
    public var feed: String
    public var folder: String?
    public var score: Double?
    public var band: String?
    public var lesson: String?
    public var concept: String?
    public var judgedAt: String?
    public var status: String?
}

public struct ListAnswer: Codable, Equatable {
    public var schema: Int
    public var band: Double
    public var folder: String?
    public var articles: [Article]
}

public struct FindAnswer: Codable, Equatable {
    public var schema: Int
    public var query: String
    public var unread: Bool
    public var hits: [Article]
}

public struct ArticleView: Codable, Equatable {
    public var id: String
    public var title: String
    public var url: String
    public var feed: String
    public var folder: String?
    public var judged: Bool
    public var score: Double?
    public var baseScore: Double?
    public var reviewScore: Double?
    public var quality: Int?
    public var learning: Int?
    public var fit: Int?
    public var breadth: Int?
    public var relevance: Int?
    public var confidence: String?
    public var kind: String?
    public var flags: [String]
    public var lesson: String?
    public var concept: String?
    public var judgedBy: String?
    public var promptVersion: String?
    public var band: String?
    public var status: String?
    public var cutMode: String?
    public var cutAt: String?
    public var cutRunId: Int?
    public var starred: Bool
    public var protected: Bool
    public var newGround: Bool
    public var record: [Mark]

    public struct Mark: Codable, Equatable {
        public var kind: String
        public var at: String
    }
}

public struct WhyAnswer: Codable, Equatable {
    public var schema: Int
    public var article: ArticleView
}

public struct TagInfo: Codable, Equatable, Identifiable {
    public var tag: String
    public var group: String
    public var writer: String
    public var meaning: String
    public var id: String { tag }
}

public struct TagsAnswer: Codable, Equatable {
    public var schema: Int
    public var tags: [TagInfo]
}

// -- settings -----------------------------------------------------------------------

public struct SettingsGroup: Codable, Equatable, Identifiable {
    public var key: String
    public var title: String
    public var help: String
    public var id: String { key }
}

public struct Setting: Codable, Equatable, Identifiable {
    public var key: String
    public var title: String
    public var help: String
    public var type: String  // bool, int, float, str, path, dict
    public var control: String  // switch, number, text, choice, model, tag, path, timezone, weights, bands, prices, folders
    public var group: String
    public var `default`: JSONValue
    public var unit: String?
    public var min: Double?
    public var max: Double?
    public var choices: [String]?
    public var nullable: Bool
    public var advanced: Bool
    public var columns: [String]?
    public var fields: [Setting]?
    public var value: JSONValue?
    public var source: String?

    public var id: String { key }
    public var isInteger: Bool { type == "int" }
}

public struct SettingsSchema: Codable, Equatable {
    public var schema: Int
    public var version: Int
    public var groups: [SettingsGroup]
    public var settings: [Setting]

    public func settings(in group: String) -> [Setting] { settings.filter { $0.group == group } }
}

public struct SettingsAnswer: Codable, Equatable {
    public var schema: Int
    public var settings: [String: JSONValue]
    public var sources: [String: String]
    public var path: String
    public var oldKeys: [String]
    public var version: Int
    public var ok: Bool?
    public var changed: [String]?
    public var notes: [String]?
}

public struct SettingsRefusal: Codable, Equatable {
    public struct Item: Codable, Equatable { public var key: String; public var message: String }
    public var schema: Int?
    public var ok: Bool
    public var message: String
    public var errors: [Item]?
}

// -- folders, keys, profile ---------------------------------------------------------

public struct FolderRow: Codable, Equatable, Identifiable {
    public var name: String
    public var articlesPerDay: Double
    public var feeds: Int?
    public var judge: Bool
    public var protected: Bool
    public var cutoff: Int?
    public var cutoffInEffect: Int?
    public var inConfig: Bool
    public var inAccount: Bool?
    public var id: String { name }
}

public struct FoldersAnswer: Codable, Equatable {
    public var schema: Int
    public var folders: [FolderRow]
    public var live: Bool
    public var defaultCutoff: Int
}

public struct KeyVariable: Codable, Equatable {
    public var variable: String
    public var set: Bool
    public var source: String?
}

public struct KeyRow: Codable, Equatable, Identifiable {
    public var provider: String
    public var set: Bool
    public var required: Bool
    public var variables: [KeyVariable]
    public var id: String { provider }
}

public struct KeysAnswer: Codable, Equatable {
    public var schema: Int
    public var keys: [KeyRow]
    public var envFile: Bool
    public var keychainService: String
    public var ok: Bool?

    public func row(_ provider: String) -> KeyRow? { keys.first { $0.provider == provider } }
}

public struct ProfileQuestion: Codable, Equatable, Identifiable {
    public var key: String
    public var question: String
    public var id: String { key }
}

public struct ProfileAnswer: Codable, Equatable {
    public var schema: Int
    public var about: String
    public var learned: String
    public var aboutIsExample: Bool
    public var aboutPath: String
    public var learnedPath: String
    public var version: Int?
    public var rewrittenAt: String?
    public var questions: [ProfileQuestion]
    public var written: [String]?
}

public struct DiffLine: Codable, Equatable {
    public var kind: String  // add, del, ctx
    public var text: String
}

public struct ProfileDiff: Codable, Equatable {
    public var schema: Int
    public var version: Int?
    public var dateText: String
    public var diff: [DiffLine]
}

public struct AttentionAnswer: Codable, Equatable {
    public var schema: Int
    public var attention: [Attention]
}

// -- checks and levers ----------------------------------------------------------------

public struct ReaderCheck: Codable, Equatable {
    public var schema: Int
    public var ok: Bool
    public var message: String
    public var email: String?
    public var feeds: Int?
    public var folders: [String]?
    public var unread: Int?
    public var mustReadStream: String?
    public var mustReadStreamPresent: Bool?
}

public struct ProviderCheck: Codable, Equatable {
    public var schema: Int
    public var ok: Bool
    public var message: String
    public var provider: String?
    public var model: String?
    public var cents: Double?
}

/// `{ok, message, output?}`: what a lever answers.
public struct ActionAnswer: Codable, Equatable {
    public var schema: Int?
    public var ok: Bool
    public var message: String?
    public var output: String?
    public var pausedUntil: String?
}

public struct PrunePlan: Codable, Equatable {
    public var cut: Int
    public var unreadScored: Int
    public var boundary: Double?
    public var byBand: [String: Int]
    public var byFolder: [String: Int]
    public var best: [Article]
    public var nextRunId: Int
}

public struct CatchUpPlan: Codable, Equatable {
    public var pending: Int
    public var judge: String
    public var quick: Bool
    public var estimateUsd: Double
    public var budgetLeft: Double
    public var budgetNeeded: Double
    public var allAges: Bool
}

/// The confirmation for a prune, a clear-old or a catch-up: the numbers, the sentence, the
/// button; `plan` is the prune's or the catch-up's, decoded on demand.
public struct PlanAnswer: Codable, Equatable {
    public var schema: Int
    public var ok: Bool
    public var plan: JSONValue?
    public var count: Int?
    public var days: Int?
    public var sentence: String?
    public var button: String?
    public var message: String?

    public var prunePlan: PrunePlan? { decodePlan(PrunePlan.self) }
    public var catchUpPlan: CatchUpPlan? { decodePlan(CatchUpPlan.self) }

    private func decodePlan<T: Decodable>(_ type: T.Type) -> T? {
        guard let plan = plan, let data = try? JSONEncoder().encode(plan) else { return nil }
        return try? EngineClient.decoder.decode(type, from: data)
    }
}

public struct LogAnswer: Codable, Equatable {
    public var schema: Int?
    public var lines: [String]
}

// -- events ---------------------------------------------------------------------------

/// One frame of `/api/events`: `tick` with the live state, or a named event.
public struct EngineEvent: Equatable {
    public var id: Int?
    public var name: String
    public var data: JSONValue

    public init(id: Int?, name: String, data: JSONValue) {
        self.id = id
        self.name = name
        self.data = data
    }
}

/// The `state` part of a tick, the few fields the menu bar needs.
public struct TickState: Codable, Equatable {
    public var running: Bool
    public var kind: String?
    public var phase: String
    public var sentence: String
    public var progressDone: Int?
    public var progressTotal: Int?
    public var ratePerMin: Double?
    public var etaMin: Double?
    public var mode: String
    public var modeLabel: String
    public var nextRunInS: Double?
    public var attention: Attention?
    public var levers: [String]
}

public struct Tick: Codable, Equatable {
    public var schema: Int
    public var generation: Int
    public var asOf: String
    public var state: TickState
    public var today: JSONValue?
    public var child: Status.Child?
}

public struct RunEvent: Codable, Equatable {
    public var state: String  // started, finished
    public var kind: String?
    public var runId: Int?
    public var ok: Bool?
    public var summary: String?
    public var error: String?
}

public extension EngineEvent {
    /// The frame's data as one of the models (a `tick` as `Tick`, a `run` as `RunEvent`).
    func decode<T: Decodable>(_ type: T.Type) throws -> T {
        let data = try JSONEncoder().encode(self.data)
        return try EngineClient.decoder.decode(type, from: data)
    }
}
