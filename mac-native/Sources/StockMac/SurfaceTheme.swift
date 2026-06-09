import SwiftUI
import AppKit

// ─────────────────────────────────────────────────────────────
//  SurfaceTheme — live material & effect picker.
//  Every named surface reads its style from here.
//  Settings exposes pickers → tweak live → tell Claude to hardcode.
// ─────────────────────────────────────────────────────────────

// ── Material enum ────────────────────────────────────────────

enum SurfaceMat: String, CaseIterable, Codable {
    case glassRegular          = "glass .regular"
    case glassClear            = "glass .clear"
    case glassRegularTint20    = "glass .regular tint 0.2"
    case glassRegularTint40    = "glass .regular tint 0.4"
    case glassRegularTint60    = "glass .regular tint 0.6"
    case glassRegularTint80    = "glass .regular tint 0.8"
    case glassRegularTint100   = "glass .regular tint 1.0"
    case glassClearTint20      = "glass .clear tint 0.2"
    case glassClearTint40      = "glass .clear tint 0.4"
    case glassClearTint60      = "glass .clear tint 0.6"
    case glassClearTint80      = "glass .clear tint 0.8"
    case glassClearTint100     = "glass .clear tint 1.0"
    case visMenu               = "vis .menu"
    case visMenuFade           = "vis .menu + fade"
    case visPopover            = "vis .popover"
    case visSidebar            = "vis .sidebar"
    case visHeaderView         = "vis .headerView"
    case visHudWindow          = "vis .hudWindow"
    case visHudFade            = "vis .hudWindow + fade"
    case visWindowBG           = "vis .windowBackground"
    case visSelection          = "vis .selection"
    case plainBlack10          = "plain black 0.10"
    case plainBlack20          = "plain black 0.20"
    case plainBlack40          = "plain black 0.40"
    case plainBlack60          = "plain black 0.60"
    case none                  = "none (transparent)"
}

// ── Fade config — fully adjustable via sliders ───────────────

struct FadeConfig: Codable, Equatable {
    /// Fade is disabled when false.
    var enabled: Bool = false
    /// Where the solid (opaque) zone ends, 0.0–1.0.
    var solidEnd: Double = 0.0
    /// Where the transparent zone starts, 0.0–1.0 (must be >= solidEnd).
    var fadeEnd: Double = 1.0
    /// If true: top = opaque → bottom = transparent. False = reversed.
    var topToBottom: Bool = true

    static let off = FadeConfig(enabled: false, solidEnd: 0, fadeEnd: 1, topToBottom: true)
    static let soft = FadeConfig(enabled: true, solidEnd: 0.0, fadeEnd: 1.0, topToBottom: true)
}

extension FadeConfig {
    @ViewBuilder func apply<V: View>(to content: V) -> some View {
        if !enabled {
            content
        } else {
            let opaque = Color.black
            let clear  = Color.clear
            let start  = topToBottom ? UnitPoint.top  : UnitPoint.bottom
            let end    = topToBottom ? UnitPoint.bottom : UnitPoint.top
            let s = max(0, min(solidEnd, 1.0))
            let e = max(s, min(fadeEnd, 1.0))
            content.mask(
                LinearGradient(stops: [
                    .init(color: opaque, location: 0.0),
                    .init(color: opaque, location: s),
                    .init(color: clear,  location: e),
                    .init(color: clear,  location: 1.0),
                ], startPoint: start, endPoint: end)
            )
        }
    }
}

// ── Named surfaces ───────────────────────────────────────────

enum SurfaceSlot: String, CaseIterable, Codable {
    case cards           = "Cards (Sale / TopPhoto / Group)"
    case cardInfoBg      = "Card info section (below photo)"
    case filterPanel     = "Filter panel (floating controls bar)"
    case scrollPanelBg   = "Scroll panel background (outer)"
    case statsBlock      = "Stats blocks (Today/Week…)"
    case tabBar          = "Tab bar container"
    case popup           = "Popups & modals"
    case topBar          = "Top bar"
    case topBarTitle     = "Top bar — title (layout)"
    case topBarButtons   = "Top bar — buttons (layout)"
    case statsRow        = "Stats row (layout)"
    case tabBarRow       = "Tab bar row (layout)"
    case content         = "Content area (layout)"
    case settingsPopup   = "Settings window"
    case browserPanel    = "Browser panels (sync / log / inspector)"
    case appBackground   = "App window background (material over gradient)"
    // legacy
    case downloadsUnderlay  = "Header underlay (all tabs)"
    case panelBody          = "Panel body (glass card)"
}

// ── Observable model ─────────────────────────────────────────

@Observable
final class SurfaceTheme {

    // ── Materials — dark ──────────────────────────────────────
    var slots: [SurfaceSlot: SurfaceMat] = [
        .cards           : .glassClearTint60,
        .cardInfoBg      : .none,
        .filterPanel     : .glassClearTint40,
        .scrollPanelBg   : .none,
        .statsBlock      : .glassClearTint60,
        .tabBar          : .plainBlack40,
        .popup           : .glassClearTint40,
        .topBar          : .none,
        .settingsPopup   : .glassClearTint40,
        .browserPanel    : .glassClearTint40,
        .appBackground   : .none,
        .downloadsUnderlay : .glassRegularTint20,
        .panelBody         : .visHudWindow,
    ]

    // ── Materials — light ──────────────────────────────────────
    var lightSlots: [SurfaceSlot: SurfaceMat] = [
        .cards           : .glassClear,
        .cardInfoBg      : .none,
        .filterPanel     : .glassClear,
        .scrollPanelBg   : .none,
        .statsBlock      : .glassClear,
        .tabBar          : .plainBlack10,
        .popup           : .visHeaderView,
        .topBar          : .none,
        .settingsPopup   : .visHeaderView,
        .browserPanel    : .glassClear,
        .appBackground   : .none,
        .downloadsUnderlay : .glassClearTint20,
        .panelBody         : .visHeaderView,
    ]

    // ── Radii (each surface decoupled) ─────────────────────────
    var cardRadius:        CGFloat = 16
    var cardInfoRadius:    CGFloat = 0
    var panelRadius:       CGFloat = 22   // legacy fallback
    var filterPanelRadius: CGFloat = 22
    var scrollPanelRadius: CGFloat = 22
    var topBarRadius:      CGFloat = 22
    var tabBarRadius:      CGFloat = 999
    var statsRadius:       CGFloat = 16
    var popupRadius:       CGFloat = 22
    var browserPanelRadius: CGFloat = 22
    var settingsRadius:    CGFloat = 22

    // ── Generic per-element overrides (position / size) ────────
    // Keyed by SurfaceSlot.rawValue (offsets) or a string key (sizes).
    var offsets: [String: CGPoint] = [:]
    var sizes:   [String: CGFloat] = [:]

    func off(_ slot: SurfaceSlot) -> CGSize {
        let p = offsets[slot.rawValue] ?? .zero
        return CGSize(width: p.x, height: p.y)
    }
    func size(_ key: String, _ def: CGFloat) -> CGFloat { sizes[key] ?? def }

    /// Reset one element's position to centered (offset 0).
    func resetOffset(_ slot: SurfaceSlot) { offsets[slot.rawValue] = .zero; save() }
    /// Center EVERY element (clear all position offsets).
    func centerAllOffsets() { offsets = [:]; save() }
    /// Reset one size override back to its built-in default.
    func resetSize(_ key: String) { sizes[key] = nil; save() }

    /// Bindings for the lab (auto-save on set).
    func offsetBinding(_ slot: SurfaceSlot, axis: Int) -> Binding<Double> {
        Binding(
            get: { let p = self.offsets[slot.rawValue] ?? .zero; return Double(axis == 0 ? p.x : p.y) },
            set: { v in
                var p = self.offsets[slot.rawValue] ?? .zero
                if axis == 0 { p.x = CGFloat(v) } else { p.y = CGFloat(v) }
                self.offsets[slot.rawValue] = p; self.save()
            })
    }
    func sizeBinding(_ key: String, _ def: CGFloat) -> Binding<Double> {
        Binding(
            get: { Double(self.sizes[key] ?? def) },
            set: { v in self.sizes[key] = CGFloat(v); self.save() })
    }

    // ── Spacing & layout ───────────────────────────────────────
    var cardSpacing:      CGFloat = 10
    var contentPaddingH:  CGFloat = 20
    var contentPaddingV:  CGFloat = 20
    var panelH:           CGFloat = 52    // filter bar height
    var statsSpacing:     CGFloat = 10
    var topBarHeight:     CGFloat = 36

    // ── Accent & text ──────────────────────────────────────────
    var accentHex:    String = "#0A85FF"
    var accent: Color { Color(hex: accentHex) ?? Color(red: 0.04, green: 0.52, blue: 1.0) }
    var t1Alpha:      Double = 1.0      // primary text opacity
    var t2Alpha:      Double = 0.72     // secondary
    var t3Alpha:      Double = 0.50     // tertiary / labels

    // Optional per-theme text COLORS (hex). nil = default white(dark)/black(light).
    // Keyed "<level>.<dark|light>" so light & dark are set independently.
    var textColors: [String: String] = [:]

    private func baseTextColor(_ level: Int, isDark: Bool) -> Color {
        let key = "t\(level).\(isDark ? "dark" : "light")"
        if let h = textColors[key], let c = Color(hex: h) { return c }
        return isDark ? .white : .black
    }
    /// Resolved text color = chosen color (or white/black) × the level's alpha.
    func t1(isDark: Bool) -> Color { baseTextColor(1, isDark: isDark).opacity(t1Alpha) }
    func t2(isDark: Bool) -> Color { baseTextColor(2, isDark: isDark).opacity(isDark ? t2Alpha : t2Alpha * 0.92) }
    func t3(isDark: Bool) -> Color { baseTextColor(3, isDark: isDark).opacity(isDark ? t3Alpha : t3Alpha * 0.85) }

    /// ColorPicker binding for a text level on the given theme (writes hex).
    func textColorBinding(_ level: Int, isDark: Bool) -> Binding<Color> {
        let key = "t\(level).\(isDark ? "dark" : "light")"
        return Binding(
            get: { Color(hex: self.textColors[key] ?? "") ?? (isDark ? .white : .black) },
            set: { self.textColors[key] = $0.hexString; self.save() })
    }
    func resetTextColor(_ level: Int, isDark: Bool) {
        textColors["t\(level).\(isDark ? "dark" : "light")"] = nil; save()
    }

    // ── Pills & buttons ────────────────────────────────────────
    var pillFillAlpha:   Double = 0.28
    var pillBorderAlpha: Double = 0.08
    var pillSelTint:     Double = 0.65
    var pillPadScale:    Double = 1.0   // scales every button/pill padding
    var pillRadius:      CGFloat = 999  // corner radius of every pill/button (999 = capsule)
    var pillOffsetX:     CGFloat = 0    // nudge pill/sort rows horizontally
    var pillOffsetY:     CGFloat = 0    // nudge pill/sort rows vertically
    // Active-pill fill colour. Empty = use the accent colour.
    var pillColorHex:    String = ""
    var pillColor: Color { Color(hex: pillColorHex) ?? accent }
    // Text colour of pill/button labels — stored PER THEME so light & dark are set
    // independently. Empty = theme default (white in dark, BLACK in light), so
    // light-theme pills/tabs are readable out of the box.
    var pillTextDarkHex:  String = ""
    var pillTextLightHex: String = ""
    func pillTextColor(isDark: Bool) -> Color {
        let hex = isDark ? pillTextDarkHex : pillTextLightHex
        return Color(hex: hex) ?? (isDark ? .white : .black)
    }
    /// Per-theme ColorPicker binding (writes the hex for the given theme only).
    func pillTextBinding(isDark: Bool) -> Binding<Color> {
        Binding(
            get: { Color(hex: isDark ? self.pillTextDarkHex : self.pillTextLightHex) ?? (isDark ? .white : .black) },
            set: { if isDark { self.pillTextDarkHex = $0.hexString } else { self.pillTextLightHex = $0.hexString }; self.save() })
    }

    // ── Popup ──────────────────────────────────────────────────
    var popupBlurRadius: Double = 15
    var popupDimAlpha:   Double = 0.18
    var settingsBlurRadius: Double = 8   // gaussian blur (px) behind the Settings window

    // ── Scroll ─────────────────────────────────────────────────
    var scrollBounce:    Bool   = true
    var scrollPanelFade: Bool   = true   // fade content as it goes under filter bar
    var showScrollBar:   Bool   = false  // right-side scroll indicator

    // ── Per-element refraction (interactive glass) overrides ───
    // nil for a slot → use the global glassInteractive.
    var interactiveOverrides: [String: Bool] = [:]
    func interactive(_ slot: SurfaceSlot) -> Bool {
        interactiveOverrides[slot.rawValue] ?? glassInteractive
    }
    func interactiveBinding(_ slot: SurfaceSlot) -> Binding<Bool> {
        Binding(
            get: { self.interactiveOverrides[slot.rawValue] ?? self.glassInteractive },
            set: { self.interactiveOverrides[slot.rawValue] = $0; self.save() })
    }

    // ── Fade configs ───────────────────────────────────────────
    var underlayFade: FadeConfig       = FadeConfig(enabled: true,  solidEnd: 0.05, fadeEnd: 1.0, topToBottom: true)
    var panelBodyFade: FadeConfig      = FadeConfig(enabled: true,  solidEnd: 0.99, fadeEnd: 1.0, topToBottom: false)
    var underlayFadeLight: FadeConfig  = FadeConfig(enabled: true,  solidEnd: 0.05, fadeEnd: 1.0, topToBottom: true)
    var panelBodyFadeLight: FadeConfig = FadeConfig(enabled: false, solidEnd: 0.0,  fadeEnd: 1.0, topToBottom: true)

    // Bottom edge of the scroll canvas — content fades out as it reaches the bottom
    // window edge (mirror of the top under-panel fade). solidEnd/fadeEnd are read
    // from the BOTTOM up. Off by default so existing layouts are unchanged.
    var canvasBottomFade: FadeConfig      = FadeConfig(enabled: true, solidEnd: 0.45, fadeEnd: 1.0, topToBottom: true)
    var canvasBottomFadeLight: FadeConfig = FadeConfig(enabled: true, solidEnd: 0.45, fadeEnd: 1.0, topToBottom: true)
    func fade(canvasBottom isDark: Bool) -> FadeConfig { isDark ? canvasBottomFade : canvasBottomFadeLight }

    // ── Window ─────────────────────────────────────────────────
    var glassInteractive: Bool = true
    var inactiveDim:      Double = 0.0

    // ── persistence ──────────────────────────────────────────

    private static var fileURL: URL {
        let base = ProcessInfo.processInfo.environment["STOCK_DATA_DIR"]
            .flatMap { URL(string: "file://\($0)") }
            ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("StockAutomation")
        let recipes = base.appendingPathComponent("recipes")
        try? FileManager.default.createDirectory(at: recipes, withIntermediateDirectories: true)
        return recipes.appendingPathComponent("surface_theme.json")
    }

    // EVERY field is optional: a missing/older/partially-written field must NOT
    // make the whole decode throw — otherwise load() bails and ALL settings
    // (including saved positions) silently revert to defaults. This was the
    // "positions reset after save" bug. Each field is applied only if present.
    private struct Persisted: Codable {
        var slots: [String: SurfaceMat]?; var lightSlots: [String: SurfaceMat]?
        var underlayFade: FadeConfig?; var panelBodyFade: FadeConfig?
        var underlayFadeLight: FadeConfig?; var panelBodyFadeLight: FadeConfig?
        var canvasBottomFade: FadeConfig?; var canvasBottomFadeLight: FadeConfig?
        var glassInteractive: Bool?; var inactiveDim: Double?
        var cardRadius: CGFloat?; var panelRadius: CGFloat?
        var statsRadius: CGFloat?; var popupRadius: CGFloat?
        var cardInfoRadius: CGFloat?; var filterPanelRadius: CGFloat?
        var scrollPanelRadius: CGFloat?; var topBarRadius: CGFloat?
        var tabBarRadius: CGFloat?; var browserPanelRadius: CGFloat?; var settingsRadius: CGFloat?
        var offsets: [String: CGPoint]?; var sizes: [String: CGFloat]?
        var accentHex: String?
        var cardSpacing: CGFloat?; var contentPaddingH: CGFloat?; var contentPaddingV: CGFloat?
        var panelH: CGFloat?; var statsSpacing: CGFloat?; var topBarHeight: CGFloat?
        var t1Alpha: Double?; var t2Alpha: Double?; var t3Alpha: Double?
        var pillFillAlpha: Double?; var pillBorderAlpha: Double?; var pillSelTint: Double?
        var pillPadScale: Double?
        var pillRadius: CGFloat?; var pillOffsetX: CGFloat?; var pillOffsetY: CGFloat?
        var pillColorHex: String?; var pillTextDarkHex: String?; var pillTextLightHex: String?
        var popupBlurRadius: Double?; var popupDimAlpha: Double?; var settingsBlurRadius: Double?
        var scrollBounce: Bool?; var scrollPanelFade: Bool?
        var showScrollBar: Bool?
        var textColors: [String: String]?
        var interactiveOverrides: [String: Bool]?
    }

    func load() {
        guard let data = try? Data(contentsOf: Self.fileURL),
              let p = try? JSONDecoder().decode(Persisted.self, from: data) else { return }
        for (k, v) in (p.slots ?? [:])      { if let s = SurfaceSlot(rawValue: k) { slots[s] = v } }
        for (k, v) in (p.lightSlots ?? [:]) { if let s = SurfaceSlot(rawValue: k) { lightSlots[s] = v } }
        if let v = p.underlayFade        { underlayFade = v }
        if let v = p.panelBodyFade       { panelBodyFade = v }
        if let v = p.underlayFadeLight   { underlayFadeLight = v }
        if let v = p.panelBodyFadeLight  { panelBodyFadeLight = v }
        if let v = p.canvasBottomFade      { canvasBottomFade = v }
        if let v = p.canvasBottomFadeLight { canvasBottomFadeLight = v }
        if let v = p.glassInteractive    { glassInteractive = v }
        if let v = p.inactiveDim         { inactiveDim = v }
        if let v = p.cardRadius       { cardRadius = v }
        if let v = p.panelRadius      { panelRadius = v }
        if let v = p.statsRadius      { statsRadius = v }
        if let v = p.popupRadius      { popupRadius = v }
        if let v = p.cardInfoRadius     { cardInfoRadius = v }
        if let v = p.filterPanelRadius  { filterPanelRadius = v }
        if let v = p.scrollPanelRadius  { scrollPanelRadius = v }
        if let v = p.topBarRadius       { topBarRadius = v }
        if let v = p.tabBarRadius       { tabBarRadius = v }
        if let v = p.browserPanelRadius { browserPanelRadius = v }
        if let v = p.settingsRadius     { settingsRadius = v }
        if let v = p.offsets          { offsets = v }
        if let v = p.sizes            { sizes = v }
        if let v = p.pillPadScale     { pillPadScale = v }
        if let v = p.accentHex        { accentHex = v }
        if let v = p.cardSpacing      { cardSpacing = v }
        if let v = p.contentPaddingH  { contentPaddingH = v }
        if let v = p.contentPaddingV  { contentPaddingV = v }
        if let v = p.panelH           { panelH = v }
        if let v = p.statsSpacing     { statsSpacing = v }
        if let v = p.topBarHeight     { topBarHeight = v }
        if let v = p.t1Alpha          { t1Alpha = v }
        if let v = p.t2Alpha          { t2Alpha = v }
        if let v = p.t3Alpha          { t3Alpha = v }
        if let v = p.pillFillAlpha    { pillFillAlpha = v }
        if let v = p.pillBorderAlpha  { pillBorderAlpha = v }
        if let v = p.pillSelTint      { pillSelTint = v }
        if let v = p.pillRadius       { pillRadius = v }
        if let v = p.pillOffsetX      { pillOffsetX = v }
        if let v = p.pillOffsetY      { pillOffsetY = v }
        if let v = p.pillColorHex     { pillColorHex = v }
        if let v = p.pillTextDarkHex  { pillTextDarkHex = v }
        if let v = p.pillTextLightHex { pillTextLightHex = v }
        if let v = p.popupBlurRadius  { popupBlurRadius = v }
        if let v = p.popupDimAlpha    { popupDimAlpha = v }
        if let v = p.settingsBlurRadius { settingsBlurRadius = v }
        if let v = p.scrollBounce     { scrollBounce = v }
        if let v = p.scrollPanelFade  { scrollPanelFade = v }
        if let v = p.showScrollBar    { showScrollBar = v }
        if let v = p.textColors       { textColors = v }
        if let v = p.interactiveOverrides { interactiveOverrides = v }
    }

    func save() {
        let p = Persisted(
            slots: Dictionary(uniqueKeysWithValues: slots.map { ($0.key.rawValue, $0.value) }),
            lightSlots: Dictionary(uniqueKeysWithValues: lightSlots.map { ($0.key.rawValue, $0.value) }),
            underlayFade: underlayFade, panelBodyFade: panelBodyFade,
            underlayFadeLight: underlayFadeLight, panelBodyFadeLight: panelBodyFadeLight,
            canvasBottomFade: canvasBottomFade, canvasBottomFadeLight: canvasBottomFadeLight,
            glassInteractive: glassInteractive, inactiveDim: inactiveDim,
            cardRadius: cardRadius, panelRadius: panelRadius,
            statsRadius: statsRadius, popupRadius: popupRadius,
            cardInfoRadius: cardInfoRadius, filterPanelRadius: filterPanelRadius,
            scrollPanelRadius: scrollPanelRadius, topBarRadius: topBarRadius,
            tabBarRadius: tabBarRadius, browserPanelRadius: browserPanelRadius, settingsRadius: settingsRadius,
            offsets: offsets, sizes: sizes,
            accentHex: accentHex,
            cardSpacing: cardSpacing, contentPaddingH: contentPaddingH, contentPaddingV: contentPaddingV,
            panelH: panelH, statsSpacing: statsSpacing, topBarHeight: topBarHeight,
            t1Alpha: t1Alpha, t2Alpha: t2Alpha, t3Alpha: t3Alpha,
            pillFillAlpha: pillFillAlpha, pillBorderAlpha: pillBorderAlpha, pillSelTint: pillSelTint,
            pillPadScale: pillPadScale,
            pillRadius: pillRadius, pillOffsetX: pillOffsetX, pillOffsetY: pillOffsetY,
            pillColorHex: pillColorHex, pillTextDarkHex: pillTextDarkHex, pillTextLightHex: pillTextLightHex,
            popupBlurRadius: popupBlurRadius, popupDimAlpha: popupDimAlpha, settingsBlurRadius: settingsBlurRadius,
            scrollBounce: scrollBounce, scrollPanelFade: scrollPanelFade,
            showScrollBar: showScrollBar,
            textColors: textColors,
            interactiveOverrides: interactiveOverrides)
        if let data = try? JSONEncoder().encode(p) { try? data.write(to: Self.fileURL) }
    }

    subscript(_ slot: SurfaceSlot) -> SurfaceMat {
        get { slots[slot] ?? .glassClearTint60 }
        set { slots[slot] = newValue; save() }
    }

    func mat(_ slot: SurfaceSlot, isDark: Bool) -> SurfaceMat {
        isDark ? (slots[slot] ?? .glassClearTint60) : (lightSlots[slot] ?? .glassClear)
    }

    func fade(underlay isDark: Bool) -> FadeConfig { isDark ? underlayFade : underlayFadeLight }
    func fade(body isDark: Bool)    -> FadeConfig { isDark ? panelBodyFade : panelBodyFadeLight }
}

// ── View modifier ─────────────────────────────────────────────

extension View {
    @ViewBuilder
    func surfaceMat(_ mat: SurfaceMat, interactive: Bool = true, radius: CGFloat = SurfaceKit.cardRadius) -> some View {
        let i = interactive
        switch mat {
        case .glassRegular:
            self.glassEffect(i ? .regular.interactive() : .regular, in: .rect(cornerRadius: radius))
        case .glassClear:
            self.glassEffect(i ? .clear.interactive() : .clear, in: .rect(cornerRadius: radius))
        case .glassRegularTint20:
            if i {
            self.glassEffect(.regular.interactive().tint(.black.opacity(0.2)), in: .rect(cornerRadius: radius))
        } else {
            self.glassEffect(.regular.tint(.black.opacity(0.2)), in: .rect(cornerRadius: radius))
        }
        case .glassRegularTint40:
            if i {
            self.glassEffect(.regular.interactive().tint(.black.opacity(0.4)), in: .rect(cornerRadius: radius))
        } else {
            self.glassEffect(.regular.tint(.black.opacity(0.4)), in: .rect(cornerRadius: radius))
        }
        case .glassRegularTint60:
            if i {
            self.glassEffect(.regular.interactive().tint(.black.opacity(0.6)), in: .rect(cornerRadius: radius))
        } else {
            self.glassEffect(.regular.tint(.black.opacity(0.6)), in: .rect(cornerRadius: radius))
        }
        case .glassRegularTint80:
            if i {
            self.glassEffect(.regular.interactive().tint(.black.opacity(0.8)), in: .rect(cornerRadius: radius))
        } else {
            self.glassEffect(.regular.tint(.black.opacity(0.8)), in: .rect(cornerRadius: radius))
        }
        case .glassRegularTint100:
            if i {
            self.glassEffect(.regular.interactive().tint(.black.opacity(1.0)), in: .rect(cornerRadius: radius))
        } else {
            self.glassEffect(.regular.tint(.black.opacity(1.0)), in: .rect(cornerRadius: radius))
        }
        case .glassClearTint20:
            if i {
            self.glassEffect(.clear.interactive().tint(.black.opacity(0.2)), in: .rect(cornerRadius: radius))
        } else {
            self.glassEffect(.clear.tint(.black.opacity(0.2)), in: .rect(cornerRadius: radius))
        }
        case .glassClearTint40:
            if i {
            self.glassEffect(.clear.interactive().tint(.black.opacity(0.4)), in: .rect(cornerRadius: radius))
        } else {
            self.glassEffect(.clear.tint(.black.opacity(0.4)), in: .rect(cornerRadius: radius))
        }
        case .glassClearTint60:
            if i {
            self.glassEffect(.clear.interactive().tint(.black.opacity(0.6)), in: .rect(cornerRadius: radius))
        } else {
            self.glassEffect(.clear.tint(.black.opacity(0.6)), in: .rect(cornerRadius: radius))
        }
        case .glassClearTint80:
            if i {
            self.glassEffect(.clear.interactive().tint(.black.opacity(0.8)), in: .rect(cornerRadius: radius))
        } else {
            self.glassEffect(.clear.tint(.black.opacity(0.8)), in: .rect(cornerRadius: radius))
        }
        case .glassClearTint100:
            if i {
            self.glassEffect(.clear.interactive().tint(.black.opacity(1.0)), in: .rect(cornerRadius: radius))
        } else {
            self.glassEffect(.clear.tint(.black.opacity(1.0)), in: .rect(cornerRadius: radius))
        }
        case .visMenu:
            self.background(SystemMaterial(material: .menu, radius: radius).allowsHitTesting(false))
        case .visMenuFade:
            self.background(SystemMaterial(material: .menu, radius: radius)
                .mask(LinearGradient(colors: [.black, .clear], startPoint: .top, endPoint: .bottom))
                .allowsHitTesting(false))
        case .visPopover:
            self.background(SystemMaterial(material: .popover, radius: radius).allowsHitTesting(false))
        case .visSidebar:
            self.background(SystemMaterial(material: .sidebar, radius: radius).allowsHitTesting(false))
        case .visHeaderView:
            self.background(SystemMaterial(material: .headerView, radius: radius).allowsHitTesting(false))
        case .visHudWindow:
            self.background(SystemMaterial(material: .hudWindow, radius: radius).allowsHitTesting(false))
        case .visHudFade:
            self.background(SystemMaterial(material: .hudWindow, radius: radius)
                .mask(LinearGradient(colors: [.black, .clear], startPoint: .top, endPoint: .bottom))
                .allowsHitTesting(false))
        case .visWindowBG:
            self.background(SystemMaterial(material: .windowBackground, radius: radius).allowsHitTesting(false))
        case .visSelection:
            self.background(SystemMaterial(material: .selection, radius: radius).allowsHitTesting(false))
        case .plainBlack10:
            self.background(Color.black.opacity(0.10), in: .rect(cornerRadius: radius))
        case .plainBlack20:
            self.background(Color.black.opacity(0.20), in: .rect(cornerRadius: radius))
        case .plainBlack40:
            self.background(Color.black.opacity(0.40), in: .rect(cornerRadius: radius))
        case .plainBlack60:
            self.background(Color.black.opacity(0.60), in: .rect(cornerRadius: radius))
        case .none:
            self
        }
    }
}

// ── Inactive window dim overlay ───────────────────────────────

struct InactiveDimModifier: ViewModifier {
    let dim: Double
    @State private var isActive = true

    func body(content: Content) -> some View {
        content
            .overlay(Color.black.opacity(isActive ? 0 : dim))
            .onReceive(NotificationCenter.default.publisher(for: NSWindow.didBecomeKeyNotification)) { _ in isActive = true }
            .onReceive(NotificationCenter.default.publisher(for: NSWindow.didResignKeyNotification)) { _ in isActive = false }
            .animation(.easeInOut(duration: 0.25), value: isActive)
    }
}

extension View {
    func inactiveDim(_ amount: Double) -> some View {
        modifier(InactiveDimModifier(dim: amount))
    }
}
