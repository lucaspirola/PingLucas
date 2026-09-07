import SwiftUI

/// Setup. Paste a topic, or paste the `https://ntfy.sh/<topic>` URL that
/// `pinglucas qr` prints -- both are accepted, because remembering which one
/// the field wants is not a thing anyone should have to do.
struct SettingsView: View {
    @Environment(PingStore.self) private var store
    @Environment(\.dismiss) private var dismiss

    @State private var server = ""
    @State private var topic = ""
    @State private var replyTopic = ""

    var body: some View {
        NavigationStack {
            Form {
                // A string-titled Section has no footer overload; the header
                // has to be spelled out once a footer is present.
                Section {
                    TextField("Server", text: $server)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    TextField("Topic", text: $topic)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    TextField("Reply topic", text: $replyTopic)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                } header: {
                    Text("Relay")
                } footer: {
                    Text("A topic name is a password: anyone who knows it can read your pings and answer as you. Keep it off screenshots.")
                }
            }
            .navigationTitle("Settings")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") { save() }
                }
            }
        }
        .onAppear {
            server = store.configuration.server.absoluteString
            topic = store.configuration.topic
            replyTopic = store.configuration.replyTopic
        }
    }

    private func save() {
        var configuration = store.configuration
        if let url = URL(string: server.trimmingCharacters(in: .whitespaces)), url.scheme != nil {
            configuration.server = url
        }
        configuration.topic = Self.normalise(topic)
        // The reply topic is the ping topic plus a suffix by convention, so
        // filling it in is one less thing to mistype.
        let reply = Self.normalise(replyTopic)
        configuration.replyTopic = reply.isEmpty && !configuration.topic.isEmpty
            ? configuration.topic + "-reply"
            : reply
        store.configuration = configuration
        dismiss()
    }

    /// Accept either a bare topic or the full URL the QR code encodes.
    private static func normalise(_ value: String) -> String {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed), url.scheme != nil else { return trimmed }
        return url.lastPathComponent
    }
}
