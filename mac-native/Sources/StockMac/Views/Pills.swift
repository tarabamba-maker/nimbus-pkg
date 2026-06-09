import SwiftUI

// ============================================================================
//  Pills.swift — THE single source of truth for EVERY button & pill in the app.
//
//  If a pill/button anywhere looks wrong, you tune it HERE and nowhere else.
//  Call sites only pick a *kind* — they never set padding / background / shape.
//
//  Subgroups:
//    0.  Tokens & shared surface     — colours, paddings, the dark-capsule fill
//    A.  Segmented selectors         — SegmentedGroup (stock filters / sort) + GlassTabBar
//    B.  PillButtonStyle             — the universal button style (5 kinds)
//    C.  Ready-made buttons          — CloseButton, FlowChips
// ============================================================================


// ── 0. Tokens & shared surface ──────────────────────────────────────────────

enum PillKit {
    /// Translucent dark fill of an *inactive* pill (the stock-filter strip look).
    static let fill        = Color.black.opacity(0.28)
    /// Hairline top-light border on inactive pills.
    static let border      = Color.white.opacity(0.08)
    static let borderWidth: CGFloat = 0.5
    /// Press feedback.
    static let pressScale: CGFloat = 0.96
    /// Segmented selector — rounded-rect selection (NOT a full capsule) + dividers.
    static let segPad:       CGFloat = 0          // gap between container edge & selection
    static let segItemPadH:  CGFloat = 14         // each segment's horizontal padding
    static let segItemPadV:  CGFloat = 6          // each segment's vertical padding
    /// Per-corner radii of the selection: the OUTER edge (left on the first
    /// segment, right on the last) rounds to the container capsule; the INNER
    /// edges stay nearly square.
    static let selOuterRadius: CGFloat = 999      // clamps to capsule = matches container end
    static let selInnerRadius: CGFloat = 5        // near-straight inner edge
    /// Accent-tint opacity of the selection's glass (lower = more see-through).
    static let selGlassTint: CGFloat = 0.65
    static let divider     = Color.white.opacity(0.14)
    static let dividerWidth: CGFloat = 1
    static let dividerHeight: CGFloat = 14
}

/// Selection shape that matches the container at its ends: outer corners round
/// to the capsule, inner corners stay square. `first`/`last` pick which side.
func segSelectionShape(first: Bool, last: Bool) -> UnevenRoundedRectangle {
    UnevenRoundedRectangle(
        topLeadingRadius:     first ? PillKit.selOuterRadius : PillKit.selInnerRadius,
        bottomLeadingRadius:  first ? PillKit.selOuterRadius : PillKit.selInnerRadius,
        bottomTrailingRadius: last  ? PillKit.selOuterRadius : PillKit.selInnerRadius,
        topTrailingRadius:    last  ? PillKit.selOuterRadius : PillKit.selInnerRadius,
        style: .continuous)
}

/// The blue selection fill — translucent Liquid Glass tinted with the accent,
/// so it refracts the background like the cards (not a flat gradient).
@ViewBuilder func segSelectionFill(first: Bool, last: Bool) -> some View {
    Color.clear
        .glassEffect(.clear.tint(Theme.accent.opacity(PillKit.selGlassTint)).interactive(),
                     in: segSelectionShape(first: first, last: last))
}

/// Dark capsule container for a segmented selector. `segPad` = gap to the inner
/// selection (0 = selection fills the container edge-to-edge).
struct SegmentedContainer: ViewModifier {
    @Environment(AppModel.self) private var model
    func body(content: Content) -> some View {
        let s = model.surfaces
        content
            .padding(PillKit.segPad)
            .surfaceMat(s.mat(.tabBar, isDark: model.isDark),
                        interactive: s.interactive(.tabBar), radius: s.tabBarRadius)
            .overlay(Capsule().strokeBorder(PillKit.border, lineWidth: PillKit.borderWidth))
    }
}

/// The dark-capsule surface shared by the SegmentedGroup container AND every
/// standalone pill button. `active` ⇒ accent gradient fill instead.
struct PillSurface: ViewModifier {
    var active: Bool = false
    @Environment(AppModel.self) private var model
    func body(content: Content) -> some View {
        let s = model.surfaces
        let shape = RoundedRectangle(cornerRadius: s.pillRadius, style: .continuous)
        content.background {
            if active {
                Color.clear.glassEffect(
                    .clear.tint(s.pillColor.opacity(s.pillSelTint)).interactive(),
                    in: shape)
            } else if model.isDark {
                shape.fill(Color.black.opacity(s.pillFillAlpha))
                shape.strokeBorder(Color.white.opacity(s.pillBorderAlpha), lineWidth: PillKit.borderWidth)
            } else {
                // Light theme: regular Liquid Glass tint 0.2 (the dark fill read badly).
                Color.clear.glassEffect(.regular.tint(.black.opacity(0.2)), in: shape)
            }
        }
    }
}

extension View {
    func pillSurface(active: Bool = false) -> some View { modifier(PillSurface(active: active)) }
    func segmentedContainer() -> some View { modifier(SegmentedContainer()) }
}


// ── A. Segmented selectors ──────────────────────────────────────────────────

/// A row of segments inside ONE dark-capsule container, with a sliding accent
/// capsule marking the selection. Used for stock filters & sort toggles.
struct SegmentedGroup: View {
    let options: [String]
    let selected: String
    let onSelect: (String) -> Void
    var font: Font = .system(size: 12, weight: .semibold)
    @Namespace private var ns
    @Environment(AppModel.self) private var model

    var body: some View {
        let s = model.surfaces
        HStack(spacing: 0) {
            ForEach(Array(options.enumerated()), id: \.offset) { i, opt in
                if i > 0 {
                    Rectangle()
                        .fill(PillKit.divider)
                        .frame(width: PillKit.dividerWidth, height: PillKit.dividerHeight)
                        .opacity(selected == opt || selected == options[i - 1] ? 0 : 1)
                }
                Button { withAnimation(.easeOut(duration: 0.16)) { onSelect(opt) } } label: {
                    Text(opt)
                        .font(font)
                        .padding(.horizontal, PillKit.segItemPadH).padding(.vertical, PillKit.segItemPadV)
                        .foregroundStyle(s.pillTextColor(isDark: model.isDark))
                        .background {
                            if selected == opt {
                                Color.clear
                                    .glassEffect(
                                        .clear.tint(s.pillColor.opacity(s.pillSelTint)).interactive(),
                                        in: segSelectionShape(first: i == 0, last: i == options.count - 1))
                                    .matchedGeometryEffect(id: "segsel", in: ns)
                            }
                        }
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .fixedSize(horizontal: false, vertical: true)
        .segmentedContainer()
    }
}

/// Segmented sort selector: same dark-capsule container as the stock filters, but
/// the active segment shows a ↑/↓ arrow. Tapping a different segment selects it
/// (resets to descending); tapping/double-clicking the ACTIVE segment flips the
/// direction. Used by Best Sellers & Groups (Earnings / Sales / Name).
struct SortSegmented: View {
    let options: [String]
    let selected: String
    let asc: Bool
    let onSelect: (String) -> Void
    let onToggleDir: () -> Void
    @Environment(AppModel.self) private var model

    var body: some View {
        let s = model.surfaces
        HStack(spacing: 0) {
            ForEach(Array(options.enumerated()), id: \.offset) { i, opt in
                if i > 0 {
                    Rectangle().fill(PillKit.divider)
                        .frame(width: PillKit.dividerWidth, height: PillKit.dividerHeight)
                        .opacity(selected == opt || selected == options[i - 1] ? 0 : 1)
                }
                let isSel = selected == opt
                Button {
                    if isSel { onToggleDir() } else { withAnimation(.easeOut(duration: 0.16)) { onSelect(opt) } }
                } label: {
                    HStack(spacing: 3) {
                        Text(opt)
                        if isSel {
                            Image(systemName: asc ? "arrow.up" : "arrow.down").font(.system(size: 9))
                                .contentTransition(.symbolEffect(.replace))
                                .transition(.scale.combined(with: .opacity))
                        }
                    }
                    .animation(.snappy, value: asc)
                    .font(.system(size: 12, weight: .semibold))
                    .padding(.horizontal, PillKit.segItemPadH).padding(.vertical, PillKit.segItemPadV)
                    .foregroundStyle(s.pillTextColor(isDark: model.isDark))
                    .background {
                        if isSel {
                            Color.clear.glassEffect(
                                .clear.tint(s.pillColor.opacity(s.pillSelTint)).interactive(),
                                in: segSelectionShape(first: i == 0, last: i == options.count - 1))
                        }
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .simultaneousGesture(TapGesture(count: 2).onEnded { if isSel { onToggleDir() } })
            }
        }
        .fixedSize(horizontal: false, vertical: true)
        .segmentedContainer()
    }
}

/// Legacy alias kept for call sites that use SlidingPills.
typealias SlidingPills = SegmentedGroup

/// Top tab bar — the SAME segmented pill as everything else, driven by an Int.
struct GlassTabBar: View {
    let tabs: [String]
    @Binding var selection: Int

    var body: some View {
        SegmentedGroup(
            options: tabs,
            selected: selection >= 0 && selection < tabs.count ? tabs[selection] : (tabs.first ?? ""),
            onSelect: { name in if let idx = tabs.firstIndex(of: name) { selection = idx } }
        )
    }
}


// ── B. PillButtonStyle — the universal button style ─────────────────────────
//
// Every standalone button uses this. The KIND decides padding (= the shape);
// `active` (or kind == .primary) decides the accent fill. The button's LABEL
// keeps its own text / icon / font / foreground colour — the style only owns
// the padding, the capsule surface and the press animation.
//
//   .primary  — the main call-to-action (always accent):  "Sync", "Sync All"
//   .neutral  — regular text+icon action:                 "Open Inspector", Settings rows
//   .toggle   — on/off segment-ish toggle (active fills):  sort buttons, "Ungrouped"
//   .chip     — small tag:                                 stock abbr, FlowChips, "Copy"
//   .icon     — square icon-only button:                   gear, theme, close, "+"

struct PillButtonStyle: ButtonStyle {
    enum Kind { case primary, neutral, toggle, chip, icon }
    var kind: Kind = .neutral
    var active: Bool = false

    func makeBody(configuration: Configuration) -> some View {
        _PillBody(configuration: configuration, kind: kind, active: active)
    }
}

// Helper view so @Environment works inside ButtonStyle
private struct _PillBody: View {
    let configuration: PillButtonStyle.Configuration
    let kind: PillButtonStyle.Kind
    let active: Bool
    @Environment(AppModel.self) private var model

    private var padH: CGFloat {
        switch kind { case .primary: 16; case .neutral: 14; case .toggle: 12; case .chip: 10; case .icon: 8 }
    }
    private var padV: CGFloat {
        switch kind { case .primary: 8; case .neutral: 8; case .toggle: 6; case .chip: 5; case .icon: 8 }
    }

    var body: some View {
        let s = model.surfaces
        let isActive = kind == .primary || active
        let scale = CGFloat(s.pillPadScale)
        let shape = RoundedRectangle(cornerRadius: s.pillRadius, style: .continuous)
        configuration.label
            .padding(.horizontal, padH * scale).padding(.vertical, padV * scale)
            .background {
                if isActive {
                    Color.clear.glassEffect(
                        .clear.tint(s.pillColor.opacity(s.pillSelTint)).interactive(),
                        in: shape)
                } else if model.isDark {
                    shape.fill(Color.black.opacity(s.pillFillAlpha))
                    shape.strokeBorder(Color.white.opacity(s.pillBorderAlpha),
                                          lineWidth: PillKit.borderWidth)
                } else {
                    // Light theme: regular Liquid Glass tint 0.2.
                    Color.clear.glassEffect(.regular.tint(.black.opacity(0.2)), in: shape)
                }
            }
            .contentShape(shape)
            .scaleEffect(configuration.isPressed ? PillKit.pressScale : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}

extension ButtonStyle where Self == PillButtonStyle {
    static func pill(_ kind: PillButtonStyle.Kind = .neutral, active: Bool = false) -> PillButtonStyle {
        PillButtonStyle(kind: kind, active: active)
    }
}


// ── C. Ready-made buttons ───────────────────────────────────────────────────

/// Square icon close button (no blue focus ring).
struct CloseButton: View {
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            Image(systemName: "xmark").font(.system(size: 12, weight: .bold)).foregroundStyle(.white)
        }
        .buttonStyle(.pill(.icon))
        .focusEffectDisabled()
    }
}

/// Horizontal row of removable accent chips (e.g. groups a photo belongs to).
struct FlowChips: View {
    let items: [String]
    let onTap: (String) -> Void

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(items, id: \.self) { it in
                    Button { onTap(it) } label: {
                        HStack(spacing: 4) {
                            Text(it).font(.system(size: 11)).lineLimit(1)
                            Image(systemName: "xmark").font(.system(size: 8))
                        }
                        .foregroundStyle(.white)
                    }
                    .buttonStyle(.pill(.chip, active: true))
                }
            }
        }
    }
}

// ── GlassSlider ─────────────────────────────────────────────
// Track and thumb use Liquid Glass; thumb animates on press.

struct GlassSlider: View {
    @Binding var value: Double
    var range: ClosedRange<Double> = 0...1

    @State private var pressing = false

    private let trackH:   CGFloat = 4
    private let thumbSize: CGFloat = 18

    var body: some View {
        GeometryReader { geo in
            let width    = geo.size.width
            let fraction = (value - range.lowerBound) / (range.upperBound - range.lowerBound)
            let thumbX   = thumbSize / 2 + fraction * (width - thumbSize)

            ZStack(alignment: .leading) {
                Capsule()
                    .fill(PillKit.fill)
                    .overlay(Capsule().strokeBorder(PillKit.border, lineWidth: 0.5))
                    .frame(height: trackH)

                Capsule()
                    .glassEffect(.clear.tint(Theme.accent.opacity(0.5)).interactive(), in: Capsule())
                    .frame(width: max(thumbSize / 2, thumbX), height: trackH)

                Circle()
                    .glassEffect(.clear.tint(pressing
                        ? Theme.accent.opacity(0.8)
                        : Color.black.opacity(0.1)).interactive(),
                        in: Circle())
                    .frame(width: thumbSize, height: thumbSize)
                    .scaleEffect(pressing ? 1.2 : 1.0)
                    .animation(.spring(response: 0.18, dampingFraction: 0.55), value: pressing)
                    .offset(x: thumbX - thumbSize / 2)
                    .gesture(
                        DragGesture(minimumDistance: 0)
                            .onChanged { g in
                                pressing = true
                                let raw = (g.location.x - thumbSize / 2) / (width - thumbSize)
                                value = range.lowerBound + max(0, min(1, raw)) * (range.upperBound - range.lowerBound)
                            }
                            .onEnded { _ in pressing = false }
                    )
            }
            .frame(height: thumbSize)
        }
        .frame(height: thumbSize)
    }
}
