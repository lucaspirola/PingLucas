import Foundation

/// Talks to the ntfy topics the relay publishes on.
///
/// Deliberately plain HTTPS with no push entitlement: a free Apple ID cannot
/// obtain `aps-environment`, so an app installed that way can never receive
/// APNs. Polling on foreground and on background refresh is what is actually
/// available, and it is enough for a queue that is usually one item deep.
actor RelayClient {
    struct Configuration: Sendable, Codable, Equatable {
        var server: URL = URL(string: "https://ntfy.sh")!
        var topic: String = ""
        var replyTopic: String = ""

        var isConfigured: Bool { !topic.isEmpty }
    }

    private let session: URLSession
    private var configuration: Configuration
    private var cursor: String?

    init(configuration: Configuration, session: URLSession = .shared) {
        self.configuration = configuration
        self.session = session
    }

    func update(_ configuration: Configuration) {
        guard configuration != self.configuration else { return }
        self.configuration = configuration
        cursor = nil
    }

    /// Fetch everything since the last call. The first call asks for recent
    /// history so a freshly launched watch is not blind to a question that
    /// arrived a minute ago.
    func fetch() async throws -> [Ping] {
        guard configuration.isConfigured else { return [] }
        var components = URLComponents(
            url: configuration.server.appending(path: "\(configuration.topic)/json"),
            resolvingAgainstBaseURL: false
        )!
        components.queryItems = [
            URLQueryItem(name: "poll", value: "1"),
            URLQueryItem(name: "since", value: cursor ?? "12h"),
        ]
        let (data, response) = try await session.data(from: components.url!)
        try Self.check(response)

        var pings: [Ping] = []
        for line in String(decoding: data, as: UTF8.self).split(separator: "\n") {
            guard let event = try? JSONDecoder().decode(NtfyEvent.self, from: Data(line.utf8))
            else { continue }
            cursor = event.id
            if let ping = Ping(event: event) { pings.append(ping) }
        }
        return pings
    }

    /// Answer one question. The tag prefix is what routes it back to the exact
    /// session that asked, rather than to whichever asked most recently.
    func reply(_ text: String, tag: String) async throws {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !configuration.replyTopic.isEmpty else { return }
        var request = URLRequest(
            url: configuration.server.appending(path: configuration.replyTopic)
        )
        request.httpMethod = "POST"
        request.httpBody = Data((tag.isEmpty ? trimmed : "\(tag) \(trimmed)").utf8)
        let (_, response) = try await session.data(for: request)
        try Self.check(response)
    }

    private static func check(_ response: URLResponse) throws {
        guard let http = response as? HTTPURLResponse else { return }
        guard (200..<300).contains(http.statusCode) else {
            throw RelayError.http(http.statusCode)
        }
    }
}

enum RelayError: LocalizedError {
    case http(Int)

    var errorDescription: String? {
        switch self {
        case .http(let code): "The relay returned HTTP \(code)."
        }
    }
}
