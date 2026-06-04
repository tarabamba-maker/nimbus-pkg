import SwiftUI

struct BestSellersView: View {
    @Environment(AppModel.self) private var model
    @State private var popup: TopPhoto?
    @State private var assign: AssignTarget?
    @State private var ungrouped = false
    @State private var sortAsc = false
    @State private var pendingMatch: String?     // asset_id awaiting a link target
    @State private var matchMsg = ""
    private let cols = [GridItem(.adaptive(minimum: 168, maximum: 168), spacing: 10)]
    private let panelH: CGFloat = 52

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
            LazyVGrid(columns: cols, spacing: 10) {
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
                                RoundedRectangle(cornerRadius: 14).strokeBorder(Theme.accent, lineWidth: 2)
                            }
                        }
                }
            }
            Color.clear.frame(height: 1)
                .onAppear { Task { await store.loadPage() } }
            if store.loading { ProgressView().padding() }
        }
        .task { await store.ensure() }
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
            ForEach(["Earnings", "Sales"], id: \.self) { label in
                let isOn = (label == "Sales") == (store.sort == "count")
                Button {
                    let newSort = (label == "Sales") ? "count" : "earnings"
                    if store.sort == newSort { sortAsc.toggle() }          // repeat click → toggle dir
                    else { store.sort = newSort; sortAsc = false }
                    Task { await store.ensure() }
                } label: {
                    HStack(spacing: 3) {
                        Text(label)
                        if isOn { Image(systemName: sortAsc ? "arrow.up" : "arrow.down").font(.system(size: 9)) }
                    }
                    .font(.system(size: 12, weight: .semibold))
                    .padding(.horizontal, 12).padding(.vertical, 5)
                    .foregroundStyle(isOn ? .white : Theme.t2)
                }
                .buttonStyle(.plain).glassPill(active: isOn)
            }
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
                Text(matchMsg).font(.system(size: 11)).foregroundStyle(Theme.accent)
            }
            Spacer()
            TextField("Search ID…", text: Binding(
                get: { store.query }, set: { store.query = $0 }))
                .textFieldStyle(.plain).frame(width: 120)
                .fieldWell()
                .onSubmit { Task { await store.reloadCurrent() } }
        }
        .padding(10)
        .glassCard(16)
        .frame(height: panelH)
    }
}

struct TopPhotoCard: View {
    let photo: TopPhoto
    let byCount: Bool
    @Environment(AppModel.self) private var model

    private var groupLabel: String? { model.groups(for: photo.asset_id).first }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ZStack(alignment: .topLeading) {
                CachedThumb(assetID: photo.asset_id, fallback: photo.thumb_url)
                    .frame(width: 168, height: 112).clipped()
                WeightBars(byStock: photo.by_stock)
                    .frame(width: 168, height: 112)
            }
            VStack(alignment: .leading, spacing: 1) {
                Text(byCount ? "\(photo.count) sales" : photo.total.money)
                    .font(.system(size: 15, weight: .bold))
                Text(byCount ? photo.total.money : "↓\(photo.count)")
                    .font(.system(size: 10)).foregroundStyle(Theme.t2)
                if let g = groupLabel {
                    Text("📁 \(g)").font(.system(size: 8)).foregroundStyle(Theme.t3).lineLimit(1)
                }
            }
            .padding(.horizontal, 9).padding(.top, 7).padding(.bottom, 9)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(width: 168)
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .cardSurface(14)
    }
}
