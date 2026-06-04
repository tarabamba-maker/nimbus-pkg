import SwiftUI

/// Detail popup for a photo — per-stock earnings breakdown + group management
/// (toggle membership, search groups, create a new group). Mirrors the old
/// PhotoPopup.svelte.
struct PhotoPopup: View {
    let assetID: String
    let thumb: String?
    let byStock: [String: SaleStockStat]
    var onClose: () -> Void = {}
    @Environment(AppModel.self) private var model
    @State private var groupSearch = ""
    @State private var newGroup = ""

    private var total: Double { byStock.values.reduce(0) { $0 + $1.total } }
    private var count: Int { byStock.values.reduce(0) { $0 + $1.count } }
    private var memberOf: [String] { model.groups(for: assetID) }

    var body: some View {
        VStack(spacing: 0) {
            CachedThumb(assetID: assetID, fallback: thumb)
                .frame(height: 200).frame(maxWidth: .infinity).clipped()

            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    VStack(alignment: .leading) {
                        Text(total.money).font(.system(size: 20, weight: .bold))
                        Text("↓\(count) sales · ID \(assetID)").font(.system(size: 11)).foregroundStyle(Theme.t2)
                    }
                    Spacer()
                    CloseButton { onClose() }
                }

                // per-stock breakdown
                VStack(spacing: 6) {
                    ForEach(byStock.sorted { $0.value.total > $1.value.total }, id: \.key) { stock, stat in
                        HStack {
                            Circle().fill(Theme.color(for: stock)).frame(width: 9, height: 9)
                            Text(stock).font(.system(size: 12))
                            Spacer()
                            Text(stat.total.money).font(.system(size: 13, weight: .semibold)).foregroundStyle(Theme.accent)
                            Text("· \(stat.count)").font(.system(size: 11)).foregroundStyle(Theme.t2)
                        }
                    }
                }

                Divider()

                Text("GROUPS").font(.system(size: 10, weight: .bold)).foregroundStyle(Theme.t3)
                if !memberOf.isEmpty {
                    FlowChips(items: memberOf) { g in
                        Task { await model.toggleMember(group: g, assetID: assetID) }
                    }
                }

                TextField("Search groups…", text: $groupSearch)
                    .textFieldStyle(.plain).fieldWell()

                ScrollView {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(filteredGroups, id: \.self) { g in
                            Button {
                                Task { await model.toggleMember(group: g, assetID: assetID) }
                            } label: {
                                HStack {
                                    Image(systemName: memberOf.contains(g) ? "checkmark.circle.fill" : "circle")
                                        .foregroundStyle(memberOf.contains(g) ? Theme.accent : .secondary)
                                    Text(g).font(.system(size: 12)).lineLimit(1)
                                    Spacer()
                                }
                                .padding(.vertical, 3)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
                .frame(maxHeight: 140)

                HStack {
                    TextField("Create new group…", text: $newGroup)
                        .textFieldStyle(.plain)
                        .padding(.horizontal, 10).padding(.vertical, 6).glassPill()
                        .onSubmit { create() }
                    Button { create() } label: { Image(systemName: "plus") }
                        .buttonStyle(.plain).padding(8).glassPill(active: true)
                }
            }
            .padding(18)
        }
        .clipShape(RoundedRectangle(cornerRadius: 22))
        .popupChrome(width: 380, height: 640)
    }

    private var filteredGroups: [String] {
        let all = model.allGroupNames
        return groupSearch.isEmpty ? all
            : all.filter { $0.localizedCaseInsensitiveContains(groupSearch) }
    }

    private func create() {
        let name = newGroup
        newGroup = ""
        Task { await model.createGroup(name, with: assetID) }
    }
}

/// Simple wrapping chips row.
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
                        .padding(.horizontal, 9).padding(.vertical, 4)
                        .foregroundStyle(.white)
                    }
                    .buttonStyle(.plain).glassPill(active: true)
                }
            }
        }
    }
}
