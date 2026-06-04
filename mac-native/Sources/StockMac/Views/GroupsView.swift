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
    var id: String { asset_id }
}

// MARK: - View

struct GroupsView: View {
    @Environment(AppModel.self) private var model
    @State private var search = ""
    @State private var sort = "Earnings"          // Earnings | Sales | Name
    @State private var sortAsc = false
    @State private var selected: PhotoGroup?
    @State private var renaming: PhotoGroup?
    @State private var renameText = ""
    @State private var mergeSource: PhotoGroup?

    private let cols = [GridItem(.adaptive(minimum: 188, maximum: 188), spacing: 12)]
    private let panelH: CGFloat = 52
    private var store: GroupsStore { model.groupsStore }

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
                            GroupCard(group: g)
                                .onTapGesture { selected = g }
                                .contextMenu {
                                    Button("Open") { selected = g }
                                    Button("Rename…") { renameText = g.name; renaming = g }
                                    Button("Merge into…") { mergeSource = g }
                                    Divider()
                                    Button("Delete", role: .destructive) { Task { await model.deleteGroup(g.name) } }
                                }
                        }
                    }
                }
            }
        }
        .task { await store.ensure() }
        .popupHost(item: $selected) { g in GroupModal(group: g, onClose: { selected = nil }).environment(model) }
        .popupHost(item: $mergeSource) { src in
            MergePicker(source: src, groups: store.groups, onClose: { mergeSource = nil }) { target in
                Task { await model.mergeGroup(source: src.name, into: target) }
            }
        }
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
            Text("My Groups").font(.system(size: 16, weight: .bold))
            ForEach(["Earnings", "Sales", "Name"], id: \.self) { s in
                Button {
                    if sort == s { sortAsc.toggle() } else { sort = s; sortAsc = false }
                } label: {
                    HStack(spacing: 3) {
                        Text(s)
                        if sort == s { Image(systemName: sortAsc ? "arrow.up" : "arrow.down").font(.system(size: 9)) }
                    }
                    .font(.system(size: 12, weight: .semibold))
                    .padding(.horizontal, 12).padding(.vertical, 5)
                    .foregroundStyle(sort == s ? .white : .secondary)
                }
                .buttonStyle(.plain).glassPill(active: sort == s)
            }
            Text("\(store.groups.count) groups").font(.system(size: 11)).foregroundStyle(Theme.t3)
            Spacer()
            TextField("Search group…", text: $search)
                .textFieldStyle(.plain).frame(width: 160)
                .fieldWell()
        }
        .padding(10).glassCard(16).frame(height: panelH)
    }

    private var visible: [PhotoGroup] {
        var list = search.isEmpty ? store.groups
            : store.groups.filter { $0.name.localizedCaseInsensitiveContains(search) }
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

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 2) {
                ForEach(Array(group.photos.prefix(3).enumerated()), id: \.offset) { _, ph in
                    CachedThumb(assetID: ph.asset_id, fallback: ph.thumb)
                        .frame(width: 62, height: 90).clipped()
                }
                if group.photos.isEmpty { Rectangle().fill(.quaternary).frame(height: 90) }
            }
            .frame(height: 90).clipped()
            VStack(alignment: .leading, spacing: 2) {
                Text(group.name).font(.system(size: 12, weight: .semibold)).lineLimit(1)
                Text(group.total.money).font(.system(size: 14, weight: .bold)).foregroundStyle(Theme.accent)
                Text("↓\(group.sales)").font(.system(size: 10)).foregroundStyle(Theme.t3)
            }
            .padding(10)
        }
        .frame(width: 188)
        .clipShape(RoundedRectangle(cornerRadius: 16))
        .cardSurface(16)
    }
}

/// Searchable picker to choose a target group to merge the source into.
struct MergePicker: View {
    let source: PhotoGroup
    let groups: [PhotoGroup]
    var onClose: () -> Void
    let onPick: (String) -> Void
    @State private var search = ""

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
                                Text(g.total.money).font(.system(size: 11)).foregroundStyle(Theme.t2) }
                                .padding(.vertical, 4)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
        .padding(18).popupChrome(width: 420, height: 480)
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
                        .font(.system(size: 11)).foregroundStyle(Theme.t2)
                }
                Spacer()
                CloseButton { onClose() }
            }

            // per-stock breakdown
            HStack(spacing: 14) {
                ForEach((group.by_stock ?? [:]).sorted { $0.value.total > $1.value.total }, id: \.key) { stock, stat in
                    HStack(spacing: 5) {
                        Circle().fill(Theme.color(for: stock)).frame(width: 8, height: 8)
                        Text(stat.total.money).font(.system(size: 12, weight: .semibold))
                    }
                }
            }

            // sort pills
            HStack(spacing: 6) {
                ForEach(["Earnings", "Downloads", "Name"], id: \.self) { s in
                    Button {
                        if sort == s { sortAsc.toggle() } else { sort = s; sortAsc = false }
                    } label: {
                        HStack(spacing: 3) {
                            Text(s)
                            if sort == s { Image(systemName: sortAsc ? "arrow.up" : "arrow.down").font(.system(size: 9)) }
                        }
                        .font(.system(size: 11, weight: .semibold))
                        .padding(.horizontal, 10).padding(.vertical, 4)
                        .foregroundStyle(sort == s ? .white : .secondary)
                    }
                    .buttonStyle(.plain).glassPill(active: sort == s)
                }
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
                        .contextMenu {
                            Button("Add to group…") { assign = AssignTarget(id: ph.asset_id) }
                        }
                    }
                }
            }
            .topFade()
        }
        .padding(18)
        .popupChrome(width: 760, height: 600)
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
