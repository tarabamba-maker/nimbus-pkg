import SwiftUI

/// One continuous segmented bar across the bottom edge of a card image:
/// every stock is a segment whose width is proportional to its share of the
/// card's total earnings (red Adobe, orange SS, green iStock, …).
struct WeightBars: View {
    let byStock: [String: SaleStockStat]?
    @Environment(AppModel.self) private var model

    private var segs: [(stock: String, frac: Double)] {
        guard let bs = byStock else { return [] }
        let total = bs.values.reduce(0) { $0 + $1.total }
        guard total > 0 else { return [] }
        return bs.filter { $0.value.total > 0 }
            .sorted { $0.value.total > $1.value.total }
            .map { ($0.key, $0.value.total / total) }
    }

    var body: some View {
        GeometryReader { geo in
            HStack(spacing: 0) {
                ForEach(segs, id: \.stock) { s in
                    Rectangle()
                        .fill(model.color(for: s.stock))
                        .frame(width: geo.size.width * s.frac)
                }
            }
            .frame(height: 4)
            .frame(maxHeight: .infinity, alignment: .bottom)   // sit on the image's bottom edge
            .shadow(color: .black.opacity(0.35), radius: 1, y: -0.5)
        }
        .allowsHitTesting(false)
    }
}

/// Real Liquid Glass container for popups/modals (same material as panels).
struct PopupChrome: ViewModifier {
    var width: CGFloat
    var height: CGFloat
    @Environment(AppModel.self) private var model
    func body(content: Content) -> some View {
        let s = model.surfaces
        content
            .frame(width: width, height: height)
            .surfaceMat(s.mat(.popup, isDark: model.isDark),
                        interactive: s.interactive(.popup),
                        radius: s.popupRadius)
    }
}
extension View {
    func popupChrome(width: CGFloat, height: CGFloat) -> some View {
        modifier(PopupChrome(width: width, height: height))
    }
    func settingsChrome(width: CGFloat, height: CGFloat) -> some View {
        modifier(SettingsChrome(width: width, height: height))
    }
}

/// Liquid Glass container for the Settings window (own material + radius slot).
struct SettingsChrome: ViewModifier {
    var width: CGFloat
    var height: CGFloat
    @Environment(AppModel.self) private var model
    func body(content: Content) -> some View {
        let s = model.surfaces
        content
            .frame(width: width, height: height)
            .surfaceMat(s.mat(.settingsPopup, isDark: model.isDark),
                        interactive: s.interactive(.settingsPopup),
                        radius: s.settingsRadius)
    }
}

/// Presents `content` centered over a softly-blurred dim of the page.
struct PopupHost<Item: Identifiable, C: View>: ViewModifier {
    @Binding var item: Item?
    @ViewBuilder var content: (Item) -> C
    @Environment(AppModel.self) private var model

    func body(content base: Content) -> some View {
        let s = model.surfaces
        ZStack {
            base
                .blur(radius: item != nil ? s.popupBlurRadius : 0)
                .animation(.snappy(duration: 0.22), value: item?.id)
            if let it = item {
                BokehDim(dimAlpha: s.popupDimAlpha)
                    .ignoresSafeArea()
                    .onTapGesture { item = nil }
                    .transition(.opacity)
                content(it)
                    .transition(.scale(scale: 0.96).combined(with: .opacity))
            }
        }
        .animation(.snappy(duration: 0.22), value: item?.id)
    }
}

/// Light page dim for popups: ~18% darken + soft bokeh blobs (no heavy frost).
struct BokehDim: View {
    var dimAlpha: Double = 0.18
    var body: some View {
        ZStack {
            Color.black.opacity(dimAlpha)
            Circle().fill(Theme.accent.opacity(0.10)).frame(width: 260).blur(radius: 60).offset(x: -180, y: -120)
            Circle().fill(Color.purple.opacity(0.10)).frame(width: 300).blur(radius: 70).offset(x: 200, y: 140)
            Circle().fill(Color.cyan.opacity(0.08)).frame(width: 220).blur(radius: 60).offset(x: 120, y: -180)
        }
    }
}
extension View {
    func popupHost<Item: Identifiable, C: View>(item: Binding<Item?>,
                                                @ViewBuilder content: @escaping (Item) -> C) -> some View {
        modifier(PopupHost(item: item, content: content))
    }
}

/// Recessed input "well" inside a panel (not a floating pill).
struct FieldWell: ViewModifier {
    func body(content: Content) -> some View {
        content
            .padding(.horizontal, 10).padding(.vertical, 6)
            .background(.regularMaterial, in: .rect(cornerRadius: 9))
    }
}

extension View {
    func fieldWell() -> some View { modifier(FieldWell()) }
    func topFade(_ height: CGFloat = 14) -> some View { modifier(TopFade(height: height)) }
}

/// Top fade so scrolling content dissolves as it slides under a header —
/// the same "scroll under material" feel used by the main tabs, reused inside
/// cards/popups.
struct TopFade: ViewModifier {
    var height: CGFloat = 14
    func body(content: Content) -> some View {
        content.mask(
            VStack(spacing: 0) {
                LinearGradient(colors: [.clear, .black], startPoint: .top, endPoint: .bottom)
                    .frame(height: height)
                Color.black
            }
        )
    }
}

/// Identifiable wrapper so a single asset id can drive a popup host.
struct AssignTarget: Identifiable { let id: String }

/// Compact right-click popup: shows the groups a photo is already in and a
/// searchable list to quickly add/remove it. Same everywhere (Downloads,
/// BestSellers, group photos).
struct GroupAssignPopup: View {
    let assetID: String
    var onClose: () -> Void
    @Environment(AppModel.self) private var model
    @State private var search = ""
    @State private var newGroup = ""

    private var memberOf: [String] { model.groups(for: assetID) }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Add to group").font(.system(size: 14, weight: .bold))
                Spacer()
                CloseButton { onClose() }
            }
            if !memberOf.isEmpty {
                FlowChips(items: memberOf) { g in
                    Task { await model.toggleMember(group: g, assetID: assetID) }
                }
            }
            TextField("Search groups…", text: $search)
                .textFieldStyle(.plain).fieldWell()
            ScrollView {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(filtered, id: \.self) { g in
                        Button { Task { await model.toggleMember(group: g, assetID: assetID) } } label: {
                            HStack {
                                Image(systemName: memberOf.contains(g) ? "checkmark.circle.fill" : "circle")
                                    .foregroundStyle(memberOf.contains(g) ? model.accent : model.t2)
                                Text(g).font(.system(size: 12)).lineLimit(1)
                                Spacer()
                            }
                            .padding(.vertical, 3)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
            .topFade()
            HStack {
                TextField("Create new group…", text: $newGroup)
                    .textFieldStyle(.plain).fieldWell()
                    .onSubmit { create() }
                Button { create() } label: { Image(systemName: "plus") }
                    .buttonStyle(.plain).padding(8).glassPill(active: true).focusEffectDisabled()
            }
        }
        .padding(16)
        .popupChrome(width: 360, height: 460)
    }

    private var filtered: [String] {
        let all = model.allGroupNames
        return search.isEmpty ? all : all.filter { $0.localizedCaseInsensitiveContains(search) }
    }
    private func create() {
        let n = newGroup; newGroup = ""
        Task { await model.createGroup(n, with: assetID) }
    }
}

/// A scroll view whose content slides UNDER a floating frosted panel at the top,
/// fading out as it goes behind the panel (the macOS toolbar look).
struct PanelScroll<Panel: View, Content: View>: View {
    var panelHeight: CGFloat
    @ViewBuilder var panel: () -> Panel
    @ViewBuilder var content: () -> Content
    @Environment(AppModel.self) private var model

    var body: some View {
        let s = model.surfaces
        let fade = s.fade(underlay: model.isDark)
        let bottom = s.fade(canvasBottom: model.isDark)
        ZStack(alignment: .top) {
            ScrollView {
                content()
                    .padding(.top, panelHeight + 12)
            }
            .scrollIndicators(s.showScrollBar ? .visible : .never)
            .scrollBounceBehavior(s.scrollBounce ? .automatic : .basedOnSize)
            .mask(fadeMask(panelH: panelHeight, fade: fade, bottom: bottom))
            panel()
        }
    }

    /// Top band: content dissolves under the floating panel (FadeConfig top).
    /// Bottom band: content dissolves into the bottom window edge so the last row
    /// of cards isn't a hard square cut (FadeConfig canvasBottom). Both are driven
    /// from Materials Lab.
    @ViewBuilder
    private func fadeMask(panelH: CGFloat, fade: FadeConfig, bottom: FadeConfig) -> some View {
        let band = panelH + 8
        VStack(spacing: 0) {
            // ── top ──
            if fade.enabled {
                let sEnd = max(0, min(fade.solidEnd, 1))
                let fEnd = max(sEnd, min(fade.fadeEnd, 1))
                LinearGradient(stops: [
                    .init(color: .clear, location: 0),
                    .init(color: .clear, location: sEnd),
                    .init(color: .black, location: fEnd),
                    .init(color: .black, location: 1),
                ], startPoint: .top, endPoint: .bottom)
                .frame(height: band)
            } else {
                Color.black.frame(height: band)
            }
            // ── middle (fully visible) ──
            Color.black
            // ── bottom ──
            if bottom.enabled {
                let bs = max(0, min(bottom.solidEnd, 1))
                let be = max(bs, min(bottom.fadeEnd, 1))
                LinearGradient(stops: [
                    .init(color: .black, location: 0),
                    .init(color: .black, location: bs),
                    .init(color: .clear, location: be),
                    .init(color: .clear, location: 1),
                ], startPoint: .top, endPoint: .bottom)
                .frame(height: 96)
            }
        }
    }
}
