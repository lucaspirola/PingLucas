import SwiftUI

@main
struct PingLucasWatchApp: App {
    @State private var store = PingStore()

    var body: some Scene {
        WindowGroup {
            PendingListView()
                .environment(store)
                .tint(Theme.accent)
                .background(Theme.background)
        }
    }
}
