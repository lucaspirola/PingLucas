import SwiftUI

/// Answering, in as few gestures as watchOS allows.
///
/// The quick replies exist because most pings are a yes/no and dictating "yes"
/// to a watch in a meeting is worse than tapping it. The free-text field falls
/// through to dictation, Scribble, or the Series 11 keyboard.
struct AnswerView: View {
    let ping: Ping

    @Environment(PingStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var draft = ""
    @State private var isSending = false

    private static let quickReplies = ["Yes", "No", "Hold", "Go ahead", "Ask me later"]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.base) {
                VStack(alignment: .leading, spacing: Theme.Space.tight) {
                    Text(ping.body)
                        .font(.headline)
                        .foregroundStyle(Theme.ink)
                    HStack(spacing: Theme.Space.tight) {
                        Text(ping.agent).foregroundStyle(Theme.accent)
                        if !ping.tag.isEmpty {
                            Text(ping.tag)
                                .monospaced()
                                .foregroundStyle(Theme.muted)
                        }
                    }
                    .font(.caption2)
                }

                TextField("Reply", text: $draft, axis: .vertical)
                    .textFieldStyle(.plain)
                    .submitLabel(.send)
                    .onSubmit { send(draft) }

                ForEach(Self.quickReplies, id: \.self) { reply in
                    Button(reply) { send(reply) }
                        .buttonStyle(QuickReplyStyle())
                }
            }
            .padding(.horizontal, Theme.Space.tight)
        }
        .navigationTitle(ping.agent)
        .navigationBarTitleDisplayMode(.inline)
        .disabled(isSending)
        .overlay { if isSending { ProgressView().tint(Theme.accent) } }
    }

    private func send(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !isSending else { return }
        isSending = true
        Task {
            await store.reply(to: ping, text: trimmed)
            isSending = false
            dismiss()
        }
    }
}

private struct QuickReplyStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.body)
            .foregroundStyle(Theme.ink)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, Theme.Space.snug)
            .padding(.horizontal, Theme.Space.base)
            .background(
                RoundedRectangle(cornerRadius: Theme.Radius.control, style: .continuous)
                    .fill(configuration.isPressed ? Theme.accentPressed : Theme.surface)
            )
    }
}
