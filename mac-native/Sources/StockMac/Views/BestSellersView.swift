import SwiftUI
import AppKit

private struct MultiSel: Identifiable { let id = UUID() }

struct BestSellersView: View {
    @Environment(AppModel.self) private var model
    @State private var assign: AssignTarget?
    @State private var selected: Set<String> = []
    @State private var lastIdx: Int?
    @State private var multiAssign: MultiSel?
    @State private var ungrouped = false
    @State private var sortAsc = false
    @State private var pendingMatch: String?
    @State private var matchMsg = ""
    private var cardW: CGFloat { model.surfaces.size("cardW", 168) }
    private var cols: [GridItem] { [GridItem(.adaptive(minimum: cardW, maximum: cardW), spacing: model.surfaces.cardSpacing)] }
    private var panelH: CGFloat { model.surfaces.panelH }

    private var store: BestStore { model.best }

    private var visible: [TopPhoto] {
        var list = ungrouped ? store.items.filter { model.groups(for: $0.asset_id).isEmpty } : store.items
        if sortAsc { list = list.reversed() }
        return list
    }

    var body: some View {
        PanelScroll(panelHeight: panelH) {
            controls
        } content: {
            LazyVGrid(columns: cols, spacing: model.surfaces.cardSpacing) {
                ForEach(Array(visible.enumerated()), id: \.element.id) { i, p in
                    TopPhotoCard(photo: p, byCount: store.sort == "count",
                                 isSelected: selected.contains(p.asset_id))
                        .onTapGesture { handleTap(p, index: i) }
                        .contextMenu {
                            Button("Open") { model.openPhoto(assetID: p.asset_id, thumb: p.thumb_url, byStock: p.by_stock ?? [:]) }
                            Button("Add to group…") { assign = AssignTarget(id: p.asset_id) }
                            Button("Link to another photo") { pendingMatch = p.asset_id; matchMsg = "Click another photo to link" }
                        }
                        .overlay {
                            if pendingMatch == p.asset_id {
                                RoundedRectangle(cornerRadius: model.surfaces.cardRadius).strokeBorder(model.accent, lineWidth: 2)
                            }
                        }
                        .onAppear {
                            if p.id == visible.last?.id { Task { await fillVisible() } }
                        }
                }
            }
            if store.loading { ProgressView().padding() }
        }
        .task(id: model.period) { store.period = model.period; await store.ensure(); await fillVisible() }
        .onChange(of: model.syncTick) { _, _ in Task { await store.reloadCurrent() } }
        .onChange(of: ungrouped) { _, _ in Task { await fillVisible() } }
        // Multi-select action bar
        .overlay(alignment: .bottom) {
            if !selected.isEmpty {
                HStack(spacing: 10) {
                    Text("\(selected.count) selected").font(.system(size: 12, weight: .semibold)).foregroundStyle(.white)
                    Button { multiAssign = MultiSel() } label: {
                        Label("Add to group", systemImage: "folder.badge.plus").font(.system(size: 12, weight: .semibold)).foregroundStyle(.white)
                    }.buttonStyle(.pill(.primary))
                    Button { selected = [] } label: {
                        Label("Clear", systemImage: "xmark").font(.system(size: 12, weight: .semibold)).foregroundStyle(.white)
                    }.buttonStyle(.pill(.neutral))
                }
                .padding(.horizontal, 14).padding(.vertical, 9)
                .glassCard()
                .padding(.bottom, 18)
                .transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .animation(.snappy, value: selected.isEmpty)
        .popupHost(item: $assign) { t in
            GroupAssignPopup(assetID: t.id, onClose: { assign = nil }).environment(model)
        }
        .popupHost(item: $multiAssign) { _ in
            MultiAssignPopup(assetIDs: Array(selected), onDone: { selected = []; multiAssign = nil }).environment(model)
        }
    }

    /// Pages until the (possibly filtered) grid has enough cards or data runs out.
    /// With Ungrouped on, a loaded page can be 100% grouped → `visible` doesn't
    /// change → no new onAppear → infinite scroll stalls. So loop, don't load once.
    private func fillVisible() async {
        let target = visible.count + 60
        while !store.ended && visible.count < target {
            let before = store.items.count
            await store.loadPage()
            if store.items.count == before { break }   // no progress (error/guard)
        }
    }

    /// Cmd+click toggles, Shift+click range-selects, plain click opens (or clears selection).
    private func handleTap(_ p: TopPhoto, index: Int) {
        let mods = NSEvent.modifierFlags
        if mods.contains(.command) {
            if selected.contains(p.asset_id) { selected.remove(p.asset_id) }
            else { selected.insert(p.asset_id) }
            lastIdx = index
        } else if mods.contains(.shift), let last = lastIdx {
            let lo = min(last, index), hi = max(last, index)
            for item in visible[lo...hi] { selected.insert(item.asset_id) }
            lastIdx = index
        } else if !selected.isEmpty {
            selected = []
        } else if let primary = pendingMatch, primary != p.asset_id {
            Task {
                _ = try? await API.matchOverride(action: "link", primary: primary, assetID: p.asset_id)
                matchMsg = "Linked ✓"; pendingMatch = nil
                await store.reloadCurrent()
            }
        } else {
            model.openPhoto(assetID: p.asset_id, thumb: p.thumb_url, byStock: p.by_stock ?? [:])
            lastIdx = index
        }
    }

    private var controls: some View {
        HStack(spacing: 8) {
            SortSegmented(
                options: ["Earnings", "Sales"],
                selected: store.sort == "count" ? "Sales" : "Earnings",
                asc: sortAsc,
                onSelect: { sel in
                    store.sort = (sel == "Sales") ? "count" : "earnings"; sortAsc = false
                    Task { await store.ensure() }
                },
                onToggleDir: { sortAsc.toggle(); Task { await store.ensure() } })
                .offset(x: model.surfaces.pillOffsetX, y: model.surfaces.pillOffsetY)
            Divider().frame(height: 18)
            SlidingPills(options: model.stockList, selected: store.stock) { s in
                store.stock = s; Task { await store.ensure() }
            }
            Button { ungrouped.toggle() } label: {
                Label("Ungrouped", systemImage: "person.2")
                    .font(.system(size: 12, weight: .semibold))
                    .padding(.horizontal, 12).padding(.vertical, 5)
                    .foregroundStyle(ungrouped ? .white : .secondary)
            }
            .buttonStyle(.plain).glassPill(active: ungrouped)
            if pendingMatch != nil || !matchMsg.isEmpty {
                Text(matchMsg).font(.system(size: 11)).foregroundStyle(model.accent)
            }
            Spacer()
            TextField("Search ID…", text: Binding(
                get: { store.query }, set: { store.query = $0 }))
                .textFieldStyle(.plain).frame(width: 120)
                .fieldWell()
                .onSubmit { Task { await store.reloadCurrent() } }
        }
        .padding(10)
        .glassCard()
        .frame(height: panelH)
    }

}

struct TopPhotoCard: View {
    let photo: TopPhoto
    let byCount: Bool
    var isSelected: Bool = false
    @Environment(AppModel.self) private var model

    private var groupLabel: String? { model.groups(for: photo.asset_id).first }

    var body: some View {
        let s = model.surfaces
        let w = s.size("cardW", 168)
        let imgH = s.size("cardImgH", 112)
        VStack(alignment: .leading, spacing: 0) {
            ZStack(alignment: .topLeading) {
                CachedThumb(assetID: photo.thumb_aid ?? photo.asset_id, fallback: photo.thumb_url)
                    .frame(width: w, height: imgH).clipped()
                WeightBars(byStock: photo.by_stock)
                    .frame(width: w, height: imgH)
            }
            VStack(alignment: .leading, spacing: 1) {
                Text(byCount ? "\(photo.count) sales" : photo.total.money)
                    .font(.system(size: 15, weight: .bold))
                    .foregroundStyle(model.t1)
                    .contentTransition(.numericText())
                Text(byCount ? photo.total.money : "↓\(photo.count)")
                    .font(.system(size: 10)).foregroundStyle(model.t2)
                    .contentTransition(.numericText())
                if let g = groupLabel {
                    Text("📁 \(g)").font(.system(size: 8)).foregroundStyle(model.t3).lineLimit(1)
                }
            }
            .padding(.horizontal, 9).padding(.top, 7).padding(.bottom, 9)
            .frame(maxWidth: .infinity, alignment: .leading)
            .surfaceMat(s.mat(.cardInfoBg, isDark: model.isDark),
                        interactive: false, radius: s.cardInfoRadius)
        }
        .frame(width: w)
        .clipShape(RoundedRectangle(cornerRadius: s.cardRadius))
        .cardSurface()
        .overlay {
            if isSelected {
                RoundedRectangle(cornerRadius: s.cardRadius)
                    .strokeBorder(model.accent, lineWidth: 2)
                    .background(RoundedRectangle(cornerRadius: s.cardRadius).fill(model.accent.opacity(0.15)))
            }
        }
        .scaleEffect(isSelected ? 0.97 : 1.0)
        .animation(.snappy(duration: 0.15), value: isSelected)
        .scrollTransition { content, phase in
            content.opacity(phase.isIdentity ? 1 : 0.0).scaleEffect(phase.isIdentity ? 1 : 0.94)
        }
    }
}
