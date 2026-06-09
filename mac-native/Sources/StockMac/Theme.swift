import SwiftUI
import AppKit

extension Color {
    init?(hex: String) {
        let h = hex.trimmingCharacters(in: .init(charactersIn: "#"))
        guard h.count == 6, let val = UInt64(h, radix: 16) else { return nil }
        self.init(red: Double((val >> 16) & 0xFF) / 255,
                  green: Double((val >> 8) & 0xFF) / 255,
                  blue: Double(val & 0xFF) / 255)
    }
    var hexString: String {
        let c = NSColor(self).usingColorSpace(.sRGB) ?? NSColor(self)
        return String(format: "#%02X%02X%02X",
                      Int((c.redComponent * 255).rounded()),
                      Int((c.greenComponent * 255).rounded()),
                      Int((c.blueComponent * 255).rounded()))
    }
}

private func adaptiveInk(_ darkAlpha: CGFloat, _ lightAlpha: CGFloat) -> Color {
    Color(nsColor: NSColor(name: nil) { appearance in
        let isDark = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
        return isDark ? NSColor(white: 1, alpha: darkAlpha) : NSColor(white: 0, alpha: lightAlpha)
    })
}

enum Theme {
    static let accent = Color(red: 0.04, green: 0.52, blue: 1.0)

    /// Single corner radius used on every surface — buttons, strips, cards,
    /// popups, panels. Matches the macOS window corner radius (~12 px).
    static let radius: CGFloat = 12

    static let glassTint = Color.black.opacity(0.50)
    static let green  = Color(red: 0.19, green: 0.82, blue: 0.35)
    static let red    = Color(red: 1.0,  green: 0.27, blue: 0.23)

    static let stockColors: [String: Color] = [
        "Adobe Stock":   Color(red: 0.98, green: 0.20, blue: 0.13),
        "Shutterstock":  Color(red: 0.92, green: 0.30, blue: 0.10),
        "iStock":        Color(red: 0.13, green: 0.74, blue: 0.40),
        "Getty Images":  Color(red: 0.13, green: 0.74, blue: 0.40),
        "Depositphotos": Color(red: 0.20, green: 0.55, blue: 0.95),
        "Envato":        Color(red: 0.51, green: 0.78, blue: 0.32),
        "Freepik":       Color(red: 0.07, green: 0.45, blue: 0.92),
    ]

    static let stockAbbr: [String: String] = [
        "Adobe Stock": "AS", "Shutterstock": "SS", "iStock": "iS", "Getty Images": "iS",
        "Depositphotos": "DP", "Envato": "EN", "Freepik": "FP",
    ]

    static func color(for stock: String) -> Color { stockColors[stock] ?? .gray }

    static let t1 = adaptiveInk(1.0, 0.92)
    static let t2 = adaptiveInk(0.72, 0.66)
    static let t3 = adaptiveInk(0.50, 0.50)
}

// NOTE: every button/pill surface lives in Views/Pills.swift.
//       every panel/card/popup/input surface lives in Views/Surfaces.swift.
//       Theme keeps only colour & text tokens.

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
