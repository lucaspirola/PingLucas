import Foundation
import Observation

/// The app's single source of truth: what is waiting, and what has been said.
///
/// Answers are recorded locally the moment they are sent. The relay marks a
/// question answered on its own side, but ntfy gives us no read-back of that,
/// so the app remembers what it did rather than showing a question as still
/// open after you have already answered it.
@Observable
@MainActor
final class PingStore {
    private(set) var pings: [Ping] = []
    private(set) var lastError: String?
    private(set) var isRefreshing = false

    var configuration: RelayClient.Configuration {
        didSet {
            Self.save(configuration)
            Task { await client.update(configuration) }
        }
    }

    private let client: RelayClient
    private static let storageKey = "com.lucaspirola.pinglucas.configuration"
    private static let answeredKey = "com.lucaspirola.pinglucas.answered"

    init() {
        let configuration = Self.load()
        self.configuration = configuration
        self.client = RelayClient(configuration: configuration)
    }

    var pending: [Ping] {
        pings.filter { !$0.isAnswered }.sorted { $0.receivedAt > $1.receivedAt }
    }

    var history: [Ping] {
        pings.sorted { $0.receivedAt > $1.receivedAt }
    }

    func refresh() async {
        guard !isRefreshing else { return }
        isRefreshing = true
        defer { isRefreshing = false }
        do {
            let fresh = try await client.fetch()
            merge(fresh)
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
    }

    func reply(to ping: Ping, text: String) async {
        do {
            try await client.reply(text, tag: ping.tag)
            markAnswered(ping)
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
    }

    private func merge(_ fresh: [Ping]) {
        let answered = Self.loadAnswered()
        var byID = Dictionary(uniqueKeysWithValues: pings.map { ($0.id, $0) })
        for var ping in fresh {
            if let existing = byID[ping.id] { ping.answeredAt = existing.answeredAt }
            if ping.answeredAt == nil, let at = answered[ping.id] { ping.answeredAt = at }
            byID[ping.id] = ping
        }
        // A watch has no business holding a month of history in memory.
        pings = Array(byID.values.sorted { $0.receivedAt > $1.receivedAt }.prefix(50))
    }

    private func markAnswered(_ ping: Ping) {
        guard let index = pings.firstIndex(of: ping) else { return }
        let now = Date()
        pings[index].answeredAt = now
        var answered = Self.loadAnswered()
        answered[ping.id] = now
        Self.saveAnswered(answered)
    }

    // MARK: - Persistence

    private static func load() -> RelayClient.Configuration {
        guard let data = UserDefaults.standard.data(forKey: storageKey),
              let value = try? JSONDecoder().decode(RelayClient.Configuration.self, from: data)
        else { return RelayClient.Configuration() }
        return value
    }

    private static func save(_ configuration: RelayClient.Configuration) {
        guard let data = try? JSONEncoder().encode(configuration) else { return }
        UserDefaults.standard.set(data, forKey: storageKey)
    }

    private static func loadAnswered() -> [String: Date] {
        UserDefaults.standard.dictionary(forKey: answeredKey) as? [String: Date] ?? [:]
    }

    private static func saveAnswered(_ value: [String: Date]) {
        // Keep only what a 12-hour fetch window could still return.
        let cutoff = Date().addingTimeInterval(-24 * 60 * 60)
        UserDefaults.standard.set(value.filter { $0.value > cutoff }, forKey: answeredKey)
    }
}
