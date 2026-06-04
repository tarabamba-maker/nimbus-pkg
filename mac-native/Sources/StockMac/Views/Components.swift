import SwiftUI

/// Pills where the selected highlight slides between options (matchedGeometry),
/// matching the tab-bar feel. Used for Downloads/BestSellers stock filters.
struct SlidingPills: View {
    let options: [String]
    let selected: String
    let onSelect: (String) -> Void
    @Namespace private var ns

    var body: some View {
        HStack(spacing: 4) {
            ForEach(options, id: \.self) { opt in
                Text(opt)
                    .font(.system(size: 12, weight: .semibold))
                    .padding(.horizontal, 12).padding(.vertical, 5)
                    .foregroundStyle(selected == opt ? .white : .secondary)
                    .background {
                        if selected == opt {
                            Capsule().fill(Theme.accent.gradient)
                                .matchedGeometryEffect(id: "pillsel", in: ns)
                        }
                    }
                    .contentShape(Capsule())
                    .onTapGesture { withAnimation(.easeOut(duration: 0.16)) { onSelect(opt) } }
            }
        }
    }
}

/// One continuous segmented bar across the bottom edge of a card image:
/// every stock is a segment whose width is proportional to its share of the
/// card's total earnings (red Adobe, orange SS, green iStock, …).
struct WeightBars: View {
    let byStock: [String: SaleStockStat]?

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
                        .fill(Theme.color(for: s.stock))
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

/// Close button (no blue focus ring on present).
struct CloseButton: View {
    let action: () -> Void
    var body: some View {
        Button(action: action) { Image(systemName: "xmark").font(.system(size: 12, weight: .bold)) }
            .buttonStyle(.plain)
            .padding(8)
            .glassPill()
            .focusEffectDisabled()
    }
}

/// Real Liquid Glass container for popups/modals (same material as panels).
struct PopupChrome: ViewModifier {
    var width: CGFloat
    var height: CGFloat
    func body(content: Content) -> some View {
        content
            .frame(width: width, height: height)
            .glassCard(22)
    }
}
extension View {
    func popupChrome(width: CGFloat, height: CGFloat) -> some View {
        modifier(PopupChrome(width: width, height: height))
    }
}

/// Presents `content` centered over a softly-blurred dim of the page.
struct PopupHost<Item: Identifiable, C: View>: ViewModifier {
    @Binding var item: Item?
    @ViewBuilder var content: (Item) -> C

    func body(content base: Content) -> some View {
        ZStack {
            base
            if let it = item {
                BokehDim()
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
    var body: some View {
        ZStack {
            Color.black.opacity(0.18)
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

extension View {
    func fieldWell() -> some View { modifier(FieldWell()) }
    func topFade(_ height: CGFloat = 14) -> some View { modifier(TopFade(height: height)) }
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
                                    .foregroundStyle(memberOf.contains(g) ? Theme.accent : Theme.t2)
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

    var body: some View {
        ZStack(alignment: .top) {
            ScrollView {
                content()
                    .padding(.top, panelHeight + 12)
            }
            .mask(
                VStack(spacing: 0) {
                    LinearGradient(colors: [.clear, .black],
                                   startPoint: .top, endPoint: .bottom)
                        .frame(height: panelHeight + 8)
                    Color.black
                }
            )
            panel()
        }
    }
}
