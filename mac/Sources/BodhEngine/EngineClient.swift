import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// What the engine said no to, or could not be reached for. The app shows `message` as a
/// sentence and never a traceback, a port or a path from here.
public enum EngineError: Error, CustomStringConvertible {
    case notRunning
    case transport(Error)
    case http(status: Int, message: String)
    case refused(message: String, errors: [SettingsRefusal.Item])
    case decoding(Error, body: String)

    public var description: String {
        switch self {
        case .notRunning: return "The engine is not running."
        case .transport(let e): return "The engine did not answer: \(e.localizedDescription)"
        case .http(let status, let message): return message.isEmpty ? "The engine answered \(status)." : message
        case .refused(let message, _): return message
        case .decoding(_, _): return "The engine's answer could not be read."
        }
    }

    public var fieldErrors: [String: String] {
        if case .refused(_, let errors) = self {
            return Dictionary(errors.map { ($0.key, $0.message) }, uniquingKeysWith: { a, _ in a })
        }
        return [:]
    }
}

/// The engine's HTTP surface (docs/json.md), one method per route. Every call carries the
/// per-launch token; every answer is decoded from snake_case into the models.
public final class EngineClient {
    public let baseURL: URL
    public let token: String
    let session: URLSession

    public init(baseURL: URL, token: String, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.token = token
        self.session = session
    }

    public static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    public static let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.keyEncodingStrategy = .convertToSnakeCase
        return e
    }()

    // MARK: requests

    public func url(_ path: String, query: [String: String] = [:]) -> URL {
        var parts = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        parts.path = path
        if !query.isEmpty {
            parts.queryItems = query.keys.sorted().map { URLQueryItem(name: $0, value: query[$0]) }
        }
        return parts.url!
    }

    /// The request the window's web view loads: the app's page for one section, with the token
    /// in the header (the engine stamps it into the page for the page's own fetches).
    public func pageRequest(section: String?, theme: String? = nil) -> URLRequest {
        var query: [String: String] = ["mode": "app"]
        if let section = section { query["section"] = section }
        if let theme = theme { query["theme"] = theme }
        var request = URLRequest(url: url("/", query: query))
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.cachePolicy = .reloadIgnoringLocalCacheData
        return request
    }

    private func request(_ method: String, _ path: String, query: [String: String] = [:], body: Data? = nil) -> URLRequest {
        var request = URLRequest(url: url(path, query: query))
        request.httpMethod = method
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.timeoutInterval = 120
        if let body = body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return request
    }

    private func perform(_ request: URLRequest) async throws -> (Data, Int) {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw EngineError.transport(error)
        }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        return (data, status)
    }

    private func decode<T: Decodable>(_ type: T.Type, from data: Data, status: Int) throws -> T {
        if status >= 400 {
            let refusal = try? EngineClient.decoder.decode(SettingsRefusal.self, from: data)
            if status == 401 || status == 403 {  // the token, or the Host: never a field's fault
                throw EngineError.http(status: status, message: refusal?.message ?? "The engine refused the request (\(status)).")
            }
            if let refusal = refusal {
                throw EngineError.refused(message: refusal.message, errors: refusal.errors ?? [])
            }
            let text = String(decoding: data, as: UTF8.self)
            throw EngineError.http(status: status, message: text.count < 200 ? text : "")
        }
        do {
            return try EngineClient.decoder.decode(type, from: data)
        } catch {
            throw EngineError.decoding(error, body: String(decoding: data.prefix(500), as: UTF8.self))
        }
    }

    func get<T: Decodable>(_ type: T.Type, _ path: String, query: [String: String] = [:]) async throws -> T {
        let (data, status) = try await perform(request("GET", path, query: query))
        return try decode(type, from: data, status: status)
    }

    func send<T: Decodable>(_ type: T.Type, _ method: String, _ path: String, _ body: [String: JSONValue] = [:]) async throws -> T {
        let encoded = try EngineClient.encoder.encode(body)
        let (data, status) = try await perform(request(method, path, body: encoded))
        return try decode(type, from: data, status: status)
    }

    // MARK: look

    public func health() async throws -> Health { try await get(Health.self, "/api/health") }
    public func status() async throws -> Status { try await get(Status.self, "/api/status") }

    public func list(band: Double = 70, folder: String? = nil, limit: Int = 50) async throws -> ListAnswer {
        var query = ["band": String(Int(band)), "limit": String(limit)]
        if let folder = folder { query["folder"] = folder }
        return try await get(ListAnswer.self, "/api/list", query: query)
    }

    public func why(_ ref: String) async throws -> ArticleView {
        try await get(WhyAnswer.self, "/api/why", query: ["ref": ref]).article
    }

    public func find(_ words: String, unread: Bool = false, limit: Int = 50) async throws -> FindAnswer {
        try await get(FindAnswer.self, "/api/find", query: ["q": words, "unread": unread ? "1" : "0", "limit": String(limit)])
    }

    public func tags() async throws -> [TagInfo] { try await get(TagsAnswer.self, "/api/tags").tags }
    public func log(lines: Int = 120) async throws -> [String] { try await get(LogAnswer.self, "/api/log", query: ["n": String(lines)]).lines }
    public func attention() async throws -> [Attention] { try await get(AttentionAnswer.self, "/api/attention").attention }

    // MARK: settings, folders, keys, profile

    public func settings() async throws -> SettingsAnswer { try await get(SettingsAnswer.self, "/api/settings") }
    public func settingsSchema() async throws -> SettingsSchema { try await get(SettingsSchema.self, "/api/settings/schema") }

    public func update(settings changes: [String: JSONValue]) async throws -> SettingsAnswer {
        try await send(SettingsAnswer.self, "PUT", "/api/settings", ["changes": .object(changes)])
    }

    public func folders(live: Bool = false) async throws -> FoldersAnswer {
        try await get(FoldersAnswer.self, "/api/folders", query: live ? ["live": "1"] : [:])
    }

    public func keys() async throws -> KeysAnswer { try await get(KeysAnswer.self, "/api/keys") }

    public func setKey(provider: String, key: String, toEnv: Bool = false) async throws -> KeysAnswer {
        var body: [String: JSONValue] = ["key": .string(key)]
        if toEnv { body["where"] = .string("env") }
        return try await send(KeysAnswer.self, "PUT", "/api/keys/\(provider)", body)
    }

    public func setLocalService(baseURL: String, apiKey: String?) async throws -> KeysAnswer {
        var body: [String: JSONValue] = ["base_url": .string(baseURL)]
        if let apiKey = apiKey, !apiKey.isEmpty { body["api_key"] = .string(apiKey) }
        return try await send(KeysAnswer.self, "PUT", "/api/keys/openai-compatible", body)
    }

    public func setReader(email: String, password: String) async throws -> KeysAnswer {
        try await send(KeysAnswer.self, "PUT", "/api/keys/bazqux", ["email": .string(email), "password": .string(password)])
    }

    public func setHeartbeat(url: String) async throws -> KeysAnswer {
        try await send(KeysAnswer.self, "PUT", "/api/keys/healthchecks", ["url": .string(url)])
    }

    public func deleteKey(provider: String) async throws -> KeysAnswer {
        try await send(KeysAnswer.self, "DELETE", "/api/keys/\(provider)")
    }

    public func importEnv(delete: Bool) async throws -> KeysAnswer {
        try await send(KeysAnswer.self, "POST", "/api/keys/import-env", ["delete": .bool(delete)])
    }

    public func profile() async throws -> ProfileAnswer { try await get(ProfileAnswer.self, "/api/profile") }
    public func profileDiff() async throws -> ProfileDiff { try await get(ProfileDiff.self, "/api/profile/diff") }

    public func updateProfile(answers: [String: String]) async throws -> ProfileAnswer {
        try await send(ProfileAnswer.self, "PUT", "/api/profile", ["answers": .object(answers.mapValues { .string($0) })])
    }

    public func updateProfile(about: String) async throws -> ProfileAnswer {
        try await send(ProfileAnswer.self, "PUT", "/api/profile", ["about": .string(about)])
    }

    public func updateProfile(learned: String) async throws -> ProfileAnswer {
        try await send(ProfileAnswer.self, "PUT", "/api/profile", ["learned": .string(learned)])
    }

    public func dismiss(attention key: String) async throws -> [Attention] {
        try await send(AttentionAnswer.self, "POST", "/api/attention/dismiss", ["key": .string(key)]).attention
    }

    // MARK: the live checks

    public func checkReader(email: String, password: String) async throws -> ReaderCheck {
        try await send(ReaderCheck.self, "POST", "/api/check/reader", ["email": .string(email), "password": .string(password)])
    }

    public func checkProvider(provider: String, key: String?, model: String?) async throws -> ProviderCheck {
        var body: [String: JSONValue] = ["provider": .string(provider)]
        if let key = key, !key.isEmpty { body["key"] = .string(key) }
        if let model = model, !model.isEmpty { body["model"] = .string(model) }
        return try await send(ProviderCheck.self, "POST", "/api/check/provider", body)
    }

    // MARK: the levers

    public func run(mode: String = "normal", limit: Int? = nil) async throws -> ActionAnswer {
        var body: [String: JSONValue] = ["mode": .string(mode)]
        if let limit = limit { body["limit"] = .number(Double(limit)) }
        return try await send(ActionAnswer.self, "POST", "/api/run", body)
    }

    public func stop() async throws -> ActionAnswer { try await send(ActionAnswer.self, "POST", "/api/stop") }

    public func pause(minutes: Double) async throws -> ActionAnswer {
        try await send(ActionAnswer.self, "POST", "/api/pause", ["minutes": .number(minutes)])
    }

    public func pauseUntilTomorrow() async throws -> ActionAnswer {
        try await send(ActionAnswer.self, "POST", "/api/pause", ["until": .string("tomorrow")])
    }

    public func resume() async throws -> ActionAnswer {
        try await send(ActionAnswer.self, "POST", "/api/pause", ["until": .null])
    }

    public func mark(_ ref: String, _ kind: String) async throws -> ActionAnswer {
        try await send(ActionAnswer.self, "POST", "/api/mark", ["id": .string(ref), "kind": .string(kind)])
    }

    public func steer(_ direction: String, _ phrase: String) async throws -> ActionAnswer {
        try await send(ActionAnswer.self, "POST", "/api/steer", ["direction": .string(direction), "phrase": .string(phrase)])
    }

    public func prunePlan(keep: Int?) async throws -> PlanAnswer {
        var body: [String: JSONValue] = ["dry_run": .bool(true)]
        if let keep = keep { body["keep"] = .number(Double(keep)) }
        return try await send(PlanAnswer.self, "POST", "/api/prune", body)
    }

    public func prune(keep: Int?) async throws -> ActionAnswer {
        var body: [String: JSONValue] = ["confirm": .bool(true)]
        if let keep = keep { body["keep"] = .number(Double(keep)) }
        return try await send(ActionAnswer.self, "POST", "/api/prune", body)
    }

    public func undo(runId: Int? = nil) async throws -> ActionAnswer {
        var body: [String: JSONValue] = [:]
        if let runId = runId { body["run_id"] = .number(Double(runId)) }
        return try await send(ActionAnswer.self, "POST", "/api/undo", body)
    }

    public func clearOldPlan(days: Int) async throws -> PlanAnswer {
        try await send(PlanAnswer.self, "POST", "/api/clear_old", ["days": .number(Double(days)), "dry_run": .bool(true)])
    }

    public func clearOld(days: Int) async throws -> ActionAnswer {
        try await send(ActionAnswer.self, "POST", "/api/clear_old", ["days": .number(Double(days)), "confirm": .bool(true)])
    }

    public func catchUpPlan() async throws -> PlanAnswer {
        try await send(PlanAnswer.self, "POST", "/api/catch_up", ["dry_run": .bool(true)])
    }

    public func catchUp() async throws -> ActionAnswer {
        try await send(ActionAnswer.self, "POST", "/api/catch_up", ["confirm": .bool(true)])
    }

    public func safetyReset() async throws -> ActionAnswer { try await send(ActionAnswer.self, "POST", "/api/safety/reset") }

    // MARK: events

    /// The event stream, frame by frame, until the task is cancelled or the engine goes. A
    /// reconnect passes `since` (the last event id seen) so nothing is missed. A data-task
    /// delegate feeds the parser as bytes arrive (the same code on macOS and Linux).
    public func events(since: Int? = nil) -> AsyncThrowingStream<EngineEvent, Error> {
        var request = self.request("GET", "/api/events")
        request.timeoutInterval = 3600
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        if let since = since { request.setValue(String(since), forHTTPHeaderField: "Last-Event-ID") }
        return SSEStream.open(request, configuration: session.configuration)
    }
}

/// A `URLSession` data task whose bytes become parsed server-sent events on an async stream.
final class SSEStream: NSObject, URLSessionDataDelegate, @unchecked Sendable {  // the session's delegate queue serialises the callbacks
    private var parser = SSEParser()
    private var continuation: AsyncThrowingStream<EngineEvent, Error>.Continuation?
    private var session: URLSession?
    private var status = 0

    static func open(_ request: URLRequest, configuration: URLSessionConfiguration) -> AsyncThrowingStream<EngineEvent, Error> {
        let stream = SSEStream()
        return AsyncThrowingStream { continuation in
            stream.continuation = continuation
            let config = configuration.copy() as? URLSessionConfiguration ?? URLSessionConfiguration.default
            config.timeoutIntervalForRequest = 3600
            config.timeoutIntervalForResource = 86400
            let session = URLSession(configuration: config, delegate: stream, delegateQueue: nil)
            stream.session = session
            let task = session.dataTask(with: request)
            continuation.onTermination = { _ in
                task.cancel()
                session.invalidateAndCancel()
            }
            task.resume()
        }
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse, completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if status != 200 {
            continuation?.finish(throwing: EngineError.http(status: status, message: "the event stream was refused"))
            completionHandler(.cancel)
            return
        }
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        for event in parser.feed(data) {
            continuation?.yield(event)
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if let error = error {
            continuation?.finish(throwing: EngineError.transport(error))
        } else {
            continuation?.finish()
        }
        session.finishTasksAndInvalidate()
    }
}
