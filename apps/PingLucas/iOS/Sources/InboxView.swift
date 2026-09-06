import SwiftUI

/// The phone view. Its real job is setup -- the watch is where answering
/// happens -- so the empty state is a setup prompt, not an apology.
struct InboxView: View {
    @Environment(PingStore.self) private var store
    @State private var isShowingSettings = false

    var body: some View {
        NavigationStack {
            Group {
                if !store.configuration.isConfigured {
                    SetupPrompt { isShowingSettings = true }
                } else {
                    List {
                        if !store.pending.isEmpty {
                            Section("Waiting on you") {
                                ForEach(store.pending) { ping in
                                    NavigationLink(value: ping) { PingCard(ping: ping) }
                                }
                            }
                        }
                        let answered = store.history.filter(\.isAnswered)
                        if !answered.isEmpty {
                            Section("Answered") {
                                ForEach(answered) { ping in
                                    PingCard(ping: ping).opacity(0.55)
                                }
                            }
                        }
                    }
                    .listStyle(.insetGrouped)
                    .scrollContentBackground(.hidden)
                }
            }
            .background(Theme.background)
            .navigationTitle("PingLucas")
            .navigationDestination(for: Ping.self) { AnswerSheet(ping: $0) }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { isShowingSettings = true } label: {
                        Image(systemName: "gearshape")
                    }
                }
            }
            .sheet(isPresented: $isShowingSettings) { SettingsView() }
            .refreshable { await store.refresh() }
            .task { await store.refresh() }
        }
    }
}

private struct PingCard: View {
    let ping: Ping

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Space.tight) {
            Text(ping.body)
                .font(.body)
                .foregroundStyle(Theme.ink)
            HStack(spacing: Theme.Space.snug) {
                Label(ping.agent, systemImage: "terminal")
                    .foregroundStyle(Theme.accent)
                if !ping.tag.isEmpty {
                    Text(ping.tag).monospaced().foregroundStyle(Theme.muted)
                }
                Spacer()
                Text(ping.receivedAt, style: .relative).foregroundStyle(Theme.muted)
            }
            .font(.caption)
        }
        .padding(.vertical, Theme.Space.tight)
    }
}

private struct SetupPrompt: View {
    let openSettings: () -> Void

    var body: some View {
        VStack(spacing: Theme.Space.loose) {
            PingMark(size: 44)
            Text("Connect to your relay")
                .font(Theme.display(22))
                .foregroundStyle(Theme.ink)
            Text("Run `pinglucas qr` on your machine and scan the first code, or paste the topic in Settings.")
                .font(.callout)
                .foregroundStyle(Theme.muted)
                .multilineTextAlignment(.center)
            Button("Open settings", action: openSettings)
                .buttonStyle(.borderedProminent)
                .tint(Theme.accent)
        }
        .padding(Theme.Space.wide)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Theme.background)
    }
}

private struct AnswerSheet: View {
    let ping: Ping

    @Environment(PingStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var draft = ""

    var body: some View {
        Form {
            Section {
                Text(ping.body).foregroundStyle(Theme.ink)
            } header: {
                Text("\(ping.agent) asked")
            }
            Section {
                TextField("Your answer", text: $draft, axis: .vertical)
                    .lineLimit(2...6)
            }
            Section {
                Button("Send") {
                    Task {
                        await store.reply(to: ping, text: draft)
                        dismiss()
                    }
                }
                .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .navigationTitle(ping.tag.isEmpty ? "Reply" : ping.tag)
        .navigationBarTitleDisplayMode(.inline)
    }
}
