import Foundation

/// One question an agent is waiting on an answer for.
///
/// The relay puts the reply tag in the notification title as `agent [z2td]`,
/// because ntfy gives us no structured metadata channel. Parsing it back out
/// here is what lets a reply be addressed to the right session rather than
/// falling through to "whatever asked most recently".
struct Ping: Identifiable, Hashable, Sendable {
    let id: String
    let tag: String
    let agent: String
    let body: String
    let receivedAt: Date
    var answeredAt: Date?

    var isAnswered: Bool { answeredAt != nil }

    /// `saqr [z2td]` -> agent `saqr`, tag `z2td`.
    static func split(title: String) -> (agent: String, tag: String) {
        guard let open = title.lastIndex(of: "["),
              let close = title.lastIndex(of: "]"),
              open < close
        else { return (title, "") }
        let tag = String(title[title.index(after: open)..<close])
        let agent = title[title.startIndex..<open].trimmingCharacters(in: .whitespaces)
        return (agent.isEmpty ? "agent" : agent, tag)
    }
}

/// A message on the ntfy wire.
struct NtfyEvent: Decodable, Sendable {
    let id: String
    let time: TimeInterval
    let event: String
    let title: String?
    let message: String?

    var isMessage: Bool { event == "message" }
}

extension Ping {
    init?(event: NtfyEvent) {
        guard event.isMessage, let body = event.message, !body.isEmpty else { return nil }
        let (agent, tag) = Ping.split(title: event.title ?? "")
        self.init(
            id: event.id,
            tag: tag,
            agent: agent,
            body: body,
            receivedAt: Date(timeIntervalSince1970: event.time),
            answeredAt: nil
        )
    }
}
