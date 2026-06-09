import SwiftUI

struct BestSellersView: View {
    @Environment(AppModel.self) private var model
    @State private var popup: TopPhoto?
    @State private var assign: AssignTarget?
    @State private var ungrouped = false
    @State private var sortAsc = false
    @State private var pendingMatch: String?     // asset_id awaiting a link target
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
                ForEach(visible) { p in
                    TopPhotoCard(photo: p, byCount: store.sort == "count")
                        .onTapGesture { tap(p) }
                        .contextMenu {
                            Button("Open") { popup = p }
                            Button("Add to group…") { assign = AssignTarget(id: p.asset_id) }
                            Button("Link to another photo") { pendingMatch = p.asset_id; matchMsg = "Click another photo to link" }
                        }
                        .overlay {
                            if pendingMatch == p.asset_id {
                                RoundedRectangle(cornerRadius: model.surfaces.cardRadius).strokeBorder(model.accent, lineWidth: 2)
                            }
                        }
                        // infinite scroll: load next page as the last card appears
                        .onAppear {
                            if p.id == visible.last?.id { Task { await store.loadPage() } }
                        }
                }
            }
            if store.loading { ProgressView().padding() }
        }
        // Same period logic as Downloads: the Today/Week/Month/Year blocks set
        // model.period → reload Best Sellers for that period (the store key includes
        // period, so a new period = a fresh fetch).
        .task(id: model.period) { store.period = model.period; await store.ensure() }
        .popupHost(item: $popup) { p in
            PhotoPopup(assetID: p.asset_id, thumb: p.thumb_url, byStock: p.by_stock ?? [:],
                       onClose: { popup = nil }).environment(model)
        }
        .popupHost(item: $assign) { t in
            GroupAssignPopup(assetID: t.id, onClose: { assign = nil }).environment(model)
        }
    }

    private func tap(_ p: TopPhoto) {
        if let primary = pendingMatch, primary != p.asset_id {
            Task {
                _ = try? await API.matchOverride(action: "link", primary: primary, assetID: p.asset_id)
                matchMsg = "Linked ✓"; pendingMatch = nil
                await store.reloadCurrent()
            }
        } else {
            popup = p
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
    @Environment(AppModel.self) private var model

    private var groupLabel: String? { model.groups(for: photo.asset_id).first }

    var body: some View {
        let s = model.surfaces
        let w = s.size("cardW", 168)
        let imgH = s.size("cardImgH", 112)
        VStack(alignment: .leading, spacing: 0) {
            ZStack(alignment: .topLeading) {
                CachedThumb(assetID: photo.asset_id, fallback: photo.thumb_url)
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
        .scrollTransition { content, phase in
            content.opacity(phase.isIdentity ? 1 : 0.0).scaleEffect(phase.isIdentity ? 1 : 0.94)
        }
    }
}
