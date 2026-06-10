import SwiftUI

// MARK: - Models (local to Groups)

struct PhotoGroup: Codable, Identifiable, Equatable {
    var name: String
    var total: Double
    var count: Int
    var sales: Int
    var photos: [GroupPhoto]
    var by_stock: [String: SaleStockStat]?
    var id: String { name }
}

struct GroupPhoto: Codable, Identifiable, Equatable {
    var asset_id: String
    var earnings: Double?
    var sales_count: Int?
    var filename: String?
    var thumb: String?
    var by_stock: [String: SaleStockStat]?
    var id: String { asset_id }
}

// MARK: - View

struct GroupsView: View {
    @Environment(AppModel.self) private var model
    @State private var search = ""
    @State private var sort = "Earnings"          // Earnings | Sales | Name
    @State private var sortAsc = false
    @State private var renaming: PhotoGroup?
    @State private var renameText = ""
    @State private var mergePending: PhotoGroup?   // merge mode: source awaiting a target click

    /// In merge mode a card tap merges the pending source into the tapped group;
    /// otherwise it opens the group.
    private func handleGroupTap(_ g: PhotoGroup) {
        if let src = mergePending {
            if src.name != g.name { Task { await model.mergeGroup(source: src.name, into: g.name) } }
            withAnimation(.snappy) { mergePending = nil }
        } else {
            model.groupModal = g          // presented globally at RootView
        }
    }

    private var gCardW: CGFloat { model.surfaces.size("groupCardW", 188) }
    private var cols: [GridItem] { [GridItem(.adaptive(minimum: gCardW, maximum: gCardW), spacing: model.surfaces.cardSpacing)] }
    private var panelH: CGFloat { model.surfaces.panelH }
    private var store: GroupsStore { model.groupsStore }
    @State private var rebuilding = false

    var body: some View {
        Group {
            if store.loading {
                VStack { Spacer(); ProgressView("Завантаження груп…"); Spacer() }
            } else {
                PanelScroll(panelHeight: panelH) {
                    controls
                } content: {
                    LazyVGrid(columns: cols, spacing: 12) {
                        ForEach(visible) { g in
                            GroupCard(
                                group: g,
                                isMergeSource: mergePending?.name == g.name,
                                mergeMode: mergePending != nil,
                                onOpen:   { handleGroupTap(g) },
                                onRename: { renameText = g.name; renaming = g },
                                onMerge:  { withAnimation(.snappy) { mergePending = g } },
                                onDelete: { Task { await model.deleteGroup(g.name) } }
                            )
                            .onTapGesture { handleGroupTap(g) }
                            .transition(.scale.combined(with: .opacity))
                        }
                    }
                    // Smoothly reposition remaining cards when one merges away.
                    .animation(.snappy, value: store.groups)
                }
            }
        }
        .task(id: model.period) { await store.reload(period: model.period) }
        // Merge mode has NO window/banner: the source card is outlined accent and
        // other cards highlight on hover. Click a target group to merge; click the
        // source again to cancel.
        .alert("Rename group", isPresented: Binding(get: { renaming != nil }, set: { if !$0 { renaming = nil } })) {
            TextField("New name", text: $renameText)
            Button("Cancel", role: .cancel) { renaming = nil }
            Button("Rename") {
                if let g = renaming { Task { await model.renameGroup(g.name, to: renameText) } }
                renaming = nil
            }
        }
    }

    private var controls: some View {
        HStack(spacing: 8) {
            // Same stock-filter pills as Downloads/BestSellers — drives the top stat
            // blocks (model.activeStock) on the Groups tab and filters the list.
            ScrollView(.horizontal, showsIndicators: false) {
                SlidingPills(options: model.stockList, selected: model.groupsStock) { s in
                    model.groupsStock = s
                }
            }
            SortSegmented(
                options: ["Earnings", "Sales", "Name"],
                selected: sort, asc: sortAsc,
                onSelect: { sort = $0; sortAsc = false },
                onToggleDir: { sortAsc.toggle() })
                .offset(x: model.surfaces.pillOffsetX, y: model.surfaces.pillOffsetY)
            Text("\(visible.count)").font(.system(size: 11)).foregroundStyle(model.t3)
            Spacer()
            TextField("Search…", text: $search)
                .textFieldStyle(.plain).frame(width: 130).fieldWell()
            Button {
                Task { await rebuildMatches() }
            } label: {
                Label(rebuilding ? "Rebuilding…" : "Rebuild", systemImage: "arrow.triangle.2.circlepath")
                    .font(.system(size: 12, weight: .semibold)).foregroundStyle(.white)
            }
            .buttonStyle(.pill(.neutral)).disabled(rebuilding)
        }
        .padding(10).glassCard().frame(height: panelH)
    }

    private func rebuildMatches() async {
        rebuilding = true
        var req = URLRequest(url: URL(string: API.base + "/api/rebuild-matches")!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        // force: wipe state + full re-cluster (plain call early-exits "cached")
        req.httpBody = Data(#"{"force": true}"#.utf8)
        _ = try? await URLSession.shared.data(for: req)
        rebuilding = false
        store.loaded = false
        await store.reload(period: model.period)
    }

    private var visible: [PhotoGroup] {
        var list = search.isEmpty ? store.groups
            : store.groups.filter { $0.name.localizedCaseInsensitiveContains(search) }
        // Stock filter: show only groups that have earnings from the selected stock.
        if model.groupsStock != "All" {
            list = list.filter { ($0.by_stock?[model.groupsStock]?.total ?? 0) > 0 }
        }
        switch sort {
        case "Sales": list.sort { $0.sales > $1.sales }
        case "Name":  list.sort { $0.name < $1.name }
        default:      list.sort { $0.total > $1.total }
        }
        if sortAsc { list.reverse() }
        return list
    }
}

struct GroupCard: View {
    let group: PhotoGroup
    var isMergeSource: Bool = false
    var mergeMode: Bool = false
    var onOpen:   (() -> Void)? = nil
    var onRename: (() -> Void)? = nil
    var onMerge:  (() -> Void)? = nil
    var onDelete: (() -> Void)? = nil
    @State private var hovering = false
    @Environment(AppModel.self) private var model
    private var cardW: CGFloat { model.surfaces.size("groupCardW", 188) }
    private var imgH:  CGFloat { model.surfaces.size("groupImgH", 110) }

    var body: some View {
        let s = model.surfaces
        ZStack(alignment: .topTrailing) {
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: 2) {
                    ForEach(Array(group.photos.prefix(2)), id: \.id) { ph in
                        CachedThumb(assetID: ph.asset_id, fallback: ph.thumb)
                            .frame(width: cardW / 2, height: imgH).clipped()
                    }
                    if group.photos.isEmpty {
                        Rectangle().fill(.quaternary).frame(width: cardW, height: imgH)
                    }
                }
                .frame(width: cardW, height: imgH).clipped()

                VStack(alignment: .leading, spacing: 2) {
                    Text(group.name).font(.system(size: 12, weight: .semibold)).lineLimit(1)
                        .foregroundStyle(model.t1)
                    Text(group.total.money).font(.system(size: 14, weight: .bold)).foregroundStyle(model.accent)
                        .contentTransition(.numericText())
                    Text("↓\(group.sales)").font(.system(size: 10)).foregroundStyle(model.t3)
                        .contentTransition(.numericText())
                }
                .padding(10)
                .surfaceMat(s.mat(.cardInfoBg, isDark: model.isDark),
                            interactive: false, radius: s.cardInfoRadius)
            }
            .frame(width: cardW)
            .clipShape(RoundedRectangle(cornerRadius: s.cardRadius))
            .cardSurface()
            // Merge-mode affordance: the source is outlined solid; hovering any other
            // card shows it's a drop target.
            .overlay {
                if isMergeSource {
                    RoundedRectangle(cornerRadius: s.cardRadius).strokeBorder(model.accent, lineWidth: 2.5)
                } else if mergeMode && hovering {
                    RoundedRectangle(cornerRadius: s.cardRadius).strokeBorder(model.accent.opacity(0.7), lineWidth: 2)
                }
            }
            .opacity(mergeMode && !isMergeSource && !hovering ? 0.85 : 1)

            // Hover action buttons — top-right corner (hidden during merge mode so a
            // card click only ever means "merge into this group").
            if hovering && !mergeMode {
                HStack(spacing: 3) {
                    if let f = onOpen   { hBtn("arrow.up.right.square", action: f) }
                    if let f = onRename { hBtn("pencil",                action: f) }
                    if let f = onMerge  { hBtn("arrow.triangle.merge",  action: f) }
                    if let f = onDelete { hBtn("trash", danger: true,   action: f) }
                }
                .padding(5)
                .background(.black.opacity(0.6), in: .rect(cornerRadius: 8))
                .padding(6)
                .transition(.opacity.combined(with: .scale(scale: 0.88, anchor: .topTrailing)))
            }
        }
        .onHover { hovering = $0 }
        .animation(.easeInOut(duration: 0.13), value: hovering)
        .scrollTransition { content, phase in
            content.opacity(phase.isIdentity ? 1 : 0.0).scaleEffect(phase.isIdentity ? 1 : 0.94)
        }
    }

    private func hBtn(_ icon: String, danger: Bool = false, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: icon)
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(danger ? Color.red : .white)
                .frame(width: 22, height: 22)
        }
        .buttonStyle(.plain)
    }
}

/// Searchable picker to choose a target group to merge the source into.
struct MergePicker: View {
    let source: PhotoGroup
    let groups: [PhotoGroup]
    var onClose: () -> Void
    let onPick: (String) -> Void
    @State private var search = ""
    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Merge «\(source.name)» into…").font(.system(size: 14, weight: .bold)).lineLimit(1)
                Spacer()
                CloseButton { onClose() }
            }
            TextField("Search group…", text: $search)
                .textFieldStyle(.plain).fieldWell()
            ScrollView {
                VStack(alignment: .leading, spacing: 1) {
                    ForEach(targets) { g in
                        Button { onPick(g.name); onClose() } label: {
                            HStack { Text(g.name).font(.system(size: 12)).lineLimit(1); Spacer()
                                Text(g.total.money).font(.system(size: 11)).foregroundStyle(model.t2) }
                                .padding(.vertical, 4)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
        .padding(18)
        .frame(width: 420, height: 480)
        .surfaceMat(.glassRegular, interactive: model.surfaces.interactive(.popup),
                    radius: model.surfaces.popupRadius)
    }

    private var targets: [PhotoGroup] {
        groups.filter { $0.name != source.name }
            .filter { search.isEmpty || $0.name.localizedCaseInsensitiveContains(search) }
    }
}

/// Group detail: per-stock breakdown + sortable photo grid (like Best Sellers).
struct GroupModal: View {
    let group: PhotoGroup
    var onClose: () -> Void
    @Environment(AppModel.self) private var model
    @State private var sort = "Earnings"      // Earnings | Downloads | Name
    @State private var sortAsc = false
    @State private var assign: AssignTarget?
    private let cols = [GridItem(.adaptive(minimum: 132, maximum: 132), spacing: 10)]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                VStack(alignment: .leading) {
                    Text(group.name).font(.system(size: 16, weight: .bold))
                    Text("\(group.total.money) · ↓\(group.sales) · \(group.photos.count) photos")
                        .font(.system(size: 11)).foregroundStyle(model.t2)
                }
                Spacer()
                CloseButton { onClose() }
            }

            // per-stock breakdown — labelled, laid out in columns of 3 so the names fit
            LazyHGrid(rows: Array(repeating: GridItem(.fixed(20), spacing: 6, alignment: .leading), count: 3),
                      alignment: .top, spacing: 18) {
                ForEach((group.by_stock ?? [:]).sorted { $0.value.total > $1.value.total }, id: \.key) { stock, stat in
                    HStack(spacing: 5) {
                        Circle().fill(model.color(for: stock)).frame(width: 8, height: 8)
                        Text(stock).font(.system(size: 11)).foregroundStyle(model.t2).lineLimit(1)
                        Text(stat.total.money).font(.system(size: 12, weight: .semibold)).foregroundStyle(model.t1)
                    }
                }
            }
            .frame(height: 72, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .leading)

            // sort pills
            HStack(spacing: 6) {
                SortSegmented(
                    options: ["Earnings", "Downloads", "Name"],
                    selected: sort, asc: sortAsc,
                    onSelect: { sort = $0; sortAsc = false },
                    onToggleDir: { sortAsc.toggle() })
            }

            ScrollView {
                LazyVGrid(columns: cols, spacing: 10) {
                    ForEach(sortedPhotos) { ph in
                        VStack(spacing: 0) {
                            CachedThumb(assetID: ph.asset_id, fallback: ph.thumb)
                                .frame(width: 132, height: 88).clipped()
                            Text((ph.earnings ?? 0).money)
                                .font(.system(size: 11, weight: .semibold))
                                .frame(maxWidth: .infinity).padding(.vertical, 4)
                                .background(.ultraThinMaterial)
                        }
                        .clipShape(RoundedRectangle(cornerRadius: 10))
                        .shadow(color: .black.opacity(0.25), radius: 4, y: 2)
                        // EXACTLY the same action as a card tap on Downloads/BestSellers:
                        // open the shared PhotoPopup — presented globally at RootView so
                        // it sits OVER this group modal (same card, same material).
                        .onTapGesture {
                            model.openPhoto(assetID: ph.asset_id, thumb: ph.thumb, byStock: ph.by_stock ?? [:])
                        }
                        .contextMenu {
                            Button("Open") {
                                model.openPhoto(assetID: ph.asset_id, thumb: ph.thumb, byStock: ph.by_stock ?? [:])
                            }
                            Button("Add to group…") { assign = AssignTarget(id: ph.asset_id) }
                        }
                    }
                }
            }
            .topFade()
        }
        .padding(18)
        .frame(width: 760, height: 600)
        .surfaceMat(.glassRegular, interactive: model.surfaces.interactive(.popup),
                    radius: model.surfaces.popupRadius)
        .popupHost(item: $assign) { t in
            GroupAssignPopup(assetID: t.id, onClose: { assign = nil }).environment(model)
        }
    }

    private var sortedPhotos: [GroupPhoto] {
        var list = group.photos
        switch sort {
        case "Downloads": list.sort { ($0.sales_count ?? 0) > ($1.sales_count ?? 0) }
        case "Name":      list.sort { ($0.filename ?? "") < ($1.filename ?? "") }
        default:          list.sort { ($0.earnings ?? 0) > ($1.earnings ?? 0) }
        }
        if sortAsc { list.reverse() }
        return list
    }
}
