import SwiftUI
import AppKit
import UniformTypeIdentifiers

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
    @State private var uploading = false

    private var total: Double { byStock.values.reduce(0) { $0 + $1.total } }
    private var count: Int { byStock.values.reduce(0) { $0 + $1.count } }
    private var memberOf: [String] { model.groups(for: assetID) }

    var body: some View {
        VStack(spacing: 0) {
            CachedThumb(assetID: assetID, fallback: thumb)
                .frame(height: 200).frame(maxWidth: .infinity).clipped()
                // Manually set this card's image (for cards without an auto thumbnail).
                .overlay(alignment: .bottomTrailing) {
                    Button { pickAndUpload() } label: {
                        Label("Set photo", systemImage: "photo.badge.plus")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(.white)
                            .padding(.horizontal, 9).padding(.vertical, 5)
                            .background(.black.opacity(0.55), in: Capsule())
                    }
                    .buttonStyle(.plain)
                    .padding(10)
                    .opacity(uploading ? 0.5 : 1)
                    .disabled(uploading)
                }

            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    VStack(alignment: .leading) {
                        Text(total.money).font(.system(size: 20, weight: .bold))
                        Text("↓\(count) sales · ID \(assetID)").font(.system(size: 11)).foregroundStyle(model.t2)
                    }
                    Spacer()
                    CloseButton { onClose() }
                }

                // per-stock breakdown
                VStack(spacing: 6) {
                    ForEach(byStock.sorted { $0.value.total > $1.value.total }, id: \.key) { stock, stat in
                        HStack {
                            Circle().fill(model.color(for: stock)).frame(width: 9, height: 9)
                            Text(stock).font(.system(size: 12))
                            Spacer()
                            Text(stat.total.money).font(.system(size: 13, weight: .semibold)).foregroundStyle(model.accent)
                            Text("· \(stat.count)").font(.system(size: 11)).foregroundStyle(model.t2)
                        }
                    }
                }

                Divider()

                Text("GROUPS").font(.system(size: 10, weight: .bold)).foregroundStyle(model.t3)
                if !memberOf.isEmpty {
                    FlowChips(items: memberOf) { g in
                        Task { await model.toggleMember(group: g, assetID: assetID) }
                    }
                }

                TextField("Search groups…", text: $groupSearch)
                    .textFieldStyle(.plain).searchWell()

                ScrollView {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(filteredGroups, id: \.self) { g in
                            Button {
                                Task { await model.toggleMember(group: g, assetID: assetID) }
                            } label: {
                                HStack {
                                    Image(systemName: memberOf.contains(g) ? "checkmark.circle.fill" : "circle")
                                        .foregroundStyle(memberOf.contains(g) ? model.accent : .secondary)
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
                        .fieldWell()
                        .onSubmit { create() }
                    Button { create() } label: { Image(systemName: "plus").foregroundStyle(.white) }
                        .buttonStyle(.pill(.icon, active: true))
                }
            }
            .padding(18)
        }
        .clipShape(RoundedRectangle(cornerRadius: Theme.radius))
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

    /// Open a file picker, read the chosen image, and upload it as this card's thumb.
    private func pickAndUpload() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.png, .jpeg, .image]
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        guard panel.runModal() == .OK, let url = panel.url,
              let data = try? Data(contentsOf: url) else { return }
        uploading = true
        Task {
            _ = await model.uploadThumb(assetID: assetID, imageData: data)
            uploading = false
        }
    }
}

/// (FlowChips now lives in Pills.swift.)
private struct _RemovedFlowChips: View {
    var body: some View {
        EmptyView()
    }
}
