import SwiftUI
import AppKit

private func adaptiveInk(_ darkAlpha: CGFloat, _ lightAlpha: CGFloat) -> Color {
    Color(nsColor: NSColor(name: nil) { appearance in
        let isDark = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
        return isDark ? NSColor(white: 1, alpha: darkAlpha) : NSColor(white: 0, alpha: lightAlpha)
    })
}

enum Theme {
    static let accent = Color(red: 0.04, green: 0.52, blue: 1.0)
    static let green  = Color(red: 0.19, green: 0.82, blue: 0.35)
    static let red    = Color(red: 1.0,  green: 0.27, blue: 0.23)

    static let stockColors: [String: Color] = [
        "Adobe Stock":   Color(red: 0.98, green: 0.20, blue: 0.13),
        "Shutterstock":  Color(red: 0.92, green: 0.30, blue: 0.10),
        "iStock":        Color(red: 0.13, green: 0.74, blue: 0.40),
        "Depositphotos": Color(red: 0.20, green: 0.55, blue: 0.95),
        "Envato":        Color(red: 0.51, green: 0.78, blue: 0.32),
    ]

    static let stockAbbr: [String: String] = [
        "Adobe Stock": "AS", "Shutterstock": "SS", "iStock": "iS",
        "Depositphotos": "DP", "Envato": "EN",
    ]

    static func color(for stock: String) -> Color { stockColors[stock] ?? .gray }

    static let t1 = adaptiveInk(1.0, 0.92)
    static let t2 = adaptiveInk(0.72, 0.66)
    static let t3 = adaptiveInk(0.50, 0.50)
}

// ── macOS 26 Liquid Glass — one API call, system handles reflection/shadow/blur ─

/// Floating card: panels, filter rows, stat boxes, popups.
struct GlassCard: ViewModifier {
    var radius: CGFloat = 16
    var depth: CGFloat = 1          // kept for call-site compat, unused (system handles depth)
    func body(content: Content) -> some View {
        content.glassEffect(in: .rect(cornerRadius: radius))
    }
}

/// Pill: nav buttons, filter pills, icon buttons.
/// active=false → plain glass  |  active=true → accent fill (primary action)
struct GlassPill: ViewModifier {
    var active: Bool = false
    func body(content: Content) -> some View {
        if active {
            content.background(Theme.accent.gradient, in: .capsule)
        } else {
            content.glassEffect(in: .capsule)
        }
    }
}

/// Grid card surface — same glass as GlassCard, slightly tighter radius.
struct CardSurface: ViewModifier {
    var radius: CGFloat = 14
    func body(content: Content) -> some View {
        content.glassEffect(in: .rect(cornerRadius: radius))
    }
}

extension View {
    func glassCard(_ radius: CGFloat = 16, depth: CGFloat = 1) -> some View { modifier(GlassCard(radius: radius, depth: depth)) }
    func glassPill(active: Bool = false) -> some View { modifier(GlassPill(active: active)) }
    func cardSurface(_ radius: CGFloat = 14) -> some View { modifier(CardSurface(radius: radius)) }
}

extension Double {
    var money: String {
        let f = NumberFormatter()
        f.numberStyle = .decimal
        f.minimumFractionDigits = 2
        f.maximumFractionDigits = 2
        return "$" + (f.string(from: NSNumber(value: self)) ?? String(format: "%.2f", self))
    }
    var grouped: String {
        let f = NumberFormatter(); f.numberStyle = .decimal
        return f.string(from: NSNumber(value: self)) ?? "\(Int(self))"
    }
}
