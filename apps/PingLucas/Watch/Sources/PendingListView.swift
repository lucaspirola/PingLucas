import SwiftUI

/// The wrist view: what is waiting on you, newest first.
///
/// Everything here is tuned for the two seconds a raised wrist actually gets.
/// The question is the largest element; the agent that asked it is a caption,
/// not a header; and answering is one tap away with nothing in between.
struct PendingListView: View {
    @Environment(PingStore.self) private var store

    var body: some View {
        NavigationStack {
            Group {
                if !store.configuration.isConfigured {
                    UnconfiguredView()
                } else if store.pending.isEmpty {
                    QuietView(lastError: store.lastError)
                } else {
                    List {
                        ForEach(store.pending) { ping in
                            NavigationLink(value: ping) {
                                PingRow(ping: ping)
                            }
                            .listRowBackground(
                                RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous)
                                    .fill(Theme.surface)
                            )
                        }
                    }
                    .listStyle(.carousel)
                }
            }
            .navigationTitle("PingLucas")
            .navigationDestination(for: Ping.self) { ping in
                AnswerView(ping: ping)
            }
        }
        .task { await store.refresh() }
        .refreshable { await store.refresh() }
    }
}

private struct PingRow: View {
    let ping: Ping

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Space.tight) {
            Text(ping.body)
                .font(.headline)
                .foregroundStyle(Theme.ink)
                .lineLimit(4)
                .multilineTextAlignment(.leading)

            HStack(spacing: Theme.Space.tight) {
                PingMark(size: 10)
                Text(ping.agent)
                    .foregroundStyle(Theme.accent)
                Text(ping.receivedAt, style: .relative)
                    .foregroundStyle(Theme.muted)
            }
            .font(.caption2)
            .lineLimit(1)
        }
        .padding(.vertical, Theme.Space.hair)
    }
}

private struct QuietView: View {
    let lastError: String?

    var body: some View {
        VStack(spacing: Theme.Space.snug) {
            PingMark(size: 28)
            Text("Nothing waiting")
                .font(.headline)
                .foregroundStyle(Theme.ink)
            if let lastError {
                Text(lastError)
                    .font(.caption2)
                    .foregroundStyle(Theme.muted)
                    .multilineTextAlignment(.center)
            } else {
                Text("Your agents are getting on with it.")
                    .font(.caption2)
                    .foregroundStyle(Theme.muted)
                    .multilineTextAlignment(.center)
            }
        }
        .padding(Theme.Space.base)
    }
}

private struct UnconfiguredView: View {
    var body: some View {
        VStack(spacing: Theme.Space.snug) {
            PingMark(size: 28)
            Text("Not set up")
                .font(.headline)
                .foregroundStyle(Theme.ink)
            Text("Open PingLucas on your iPhone and scan the code from `pinglucas qr`.")
                .font(.caption2)
                .foregroundStyle(Theme.muted)
                .multilineTextAlignment(.center)
        }
        .padding(Theme.Space.base)
    }
}
