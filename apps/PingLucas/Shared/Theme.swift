import SwiftUI

/// Claude's visual language, as far as it can honestly be reproduced.
///
/// The colours are Anthropic's: the bone page, the clay accent, the warm
/// near-black ink. Those are the load-bearing half of the look and they carry
/// across faithfully.
///
/// The typefaces are not. Claude sets its interface in Styrene A/B and its
/// prose in Tiempos Text, both licensed from Klim Type Foundry and neither
/// redistributable in an open-source app. Rather than ship a font we have no
/// right to, the app uses the platform faces -- which on a 42mm watch is the
/// better call anyway, since SF Compact is drawn for exactly this size and a
/// borrowed grotesque would cost legibility for very little fidelity. Drop
/// licensed `.otf` files into `Resources/Fonts` and set `Theme.brandFont` if
/// you own them.
enum Theme {

    // MARK: - Colour

    /// Anthropic's palette. Light values are the brand's own; dark values are
    /// the surfaces claude.ai uses at night.
    enum Palette {
        static let bone = Color(hex: 0xF0EEE6)        // the page
        static let ivory = Color(hex: 0xFAF9F5)       // raised card, light
        static let clay = Color(hex: 0xD97757)        // the accent
        static let bookCloth = Color(hex: 0xCC785C)   // accent, pressed
        static let kraft = Color(hex: 0xD4A27F)
        static let manilla = Color(hex: 0xEBDBBC)
        static let ink = Color(hex: 0x141413)         // primary text, light
        static let slate = Color(hex: 0x1F1E1D)       // the page, dark
        static let slateRaised = Color(hex: 0x262624) // raised card, dark
        static let hairlineLight = Color(hex: 0xDAD6CA)
        static let hairlineDark = Color(hex: 0x3A3835)
        static let mutedLight = Color(hex: 0x6C6B68)
        static let mutedDark = Color(hex: 0xA3A099)
    }

    // MARK: - Semantic tokens

    /// The page. On watchOS this is always the dark surface: the display is
    /// OLED and the bone page would light a dark room from the wrist.
    static var background: Color {
        #if os(watchOS)
        Palette.slate
        #else
        Color(light: Palette.bone, dark: Palette.slate)
        #endif
    }

    static var surface: Color {
        #if os(watchOS)
        Palette.slateRaised
        #else
        Color(light: Palette.ivory, dark: Palette.slateRaised)
        #endif
    }

    static var ink: Color {
        #if os(watchOS)
        Color(hex: 0xF5F4EE)
        #else
        Color(light: Palette.ink, dark: Color(hex: 0xF5F4EE))
        #endif
    }

    static var muted: Color {
        #if os(watchOS)
        Palette.mutedDark
        #else
        Color(light: Palette.mutedLight, dark: Palette.mutedDark)
        #endif
    }

    static var hairline: Color {
        #if os(watchOS)
        Palette.hairlineDark
        #else
        Color(light: Palette.hairlineLight, dark: Palette.hairlineDark)
        #endif
    }

    static let accent = Palette.clay
    static let accentPressed = Palette.bookCloth

    // MARK: - Shape and space

    enum Radius {
        static let card: CGFloat = 12
        static let control: CGFloat = 10
        static let pill: CGFloat = 999
    }

    enum Space {
        static let hair: CGFloat = 2
        static let tight: CGFloat = 4
        static let snug: CGFloat = 8
        static let base: CGFloat = 12
        static let loose: CGFloat = 16
        static let wide: CGFloat = 24
    }

    // MARK: - Type

    /// Set this to a bundled family name if you license Styrene.
    static let brandFont: String? = nil

    static func display(_ size: CGFloat, weight: Font.Weight = .semibold) -> Font {
        if let brandFont { return .custom(brandFont, size: size).weight(weight) }
        return .system(size: size, weight: weight, design: .default)
    }
}

// MARK: - Helpers

extension Color {
    init(hex: UInt32) {
        self.init(
            .sRGB,
            red: Double((hex >> 16) & 0xFF) / 255,
            green: Double((hex >> 8) & 0xFF) / 255,
            blue: Double(hex & 0xFF) / 255,
            opacity: 1
        )
    }

    /// A colour that resolves per appearance without a colour asset catalogue,
    /// so the palette stays readable in one file.
    ///
    /// watchOS has no light appearance and no `userInterfaceStyle`, so the
    /// dynamic provider is unavailable there; the dark value is simply the
    /// value. Every `Theme` token already short-circuits on watchOS, so this
    /// branch exists for correctness rather than because it is reached.
    init(light: Color, dark: Color) {
        #if canImport(UIKit) && !os(watchOS)
        self.init(uiColor: UIColor { traits in
            traits.userInterfaceStyle == .dark ? UIColor(dark) : UIColor(light)
        })
        #elseif os(watchOS)
        self = dark
        #else
        self = light
        #endif
    }
}

/// The PingLucas mark: a ring opening toward the wearer, with the clay dot of
/// an unanswered question. Deliberately *not* Anthropic's starburst -- that is
/// their trademark and does not belong in a third-party app.
struct PingMark: View {
    var size: CGFloat = 16

    var body: some View {
        ZStack {
            Circle()
                .strokeBorder(Theme.muted.opacity(0.5), lineWidth: size / 8)
            Circle()
                .fill(Theme.accent)
                .frame(width: size / 2.6, height: size / 2.6)
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}
