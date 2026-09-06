import SwiftUI

@main
struct PingLucasApp: App {
    @State private var store = PingStore()

    var body: some Scene {
        WindowGroup {
            InboxView()
                .environment(store)
                .tint(Theme.accent)
        }
    }
}
