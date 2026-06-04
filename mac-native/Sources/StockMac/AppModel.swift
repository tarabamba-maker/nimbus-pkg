import SwiftUI
import Observation

@MainActor
@Observable
final class AppModel {
    var ready = false
    var stats = Stats()
    var stockList: [String] = ["All"]
    var period: Period = .all
    var downloadsStock = "All"
    var activeTab = 0
    var isDark = true            // theme toggle
    var bgTick = 0              // bump to reload the imported background image

    /// Path of the user-imported background for the current theme (if any).
    var backgroundURL: URL {
        Backend.shared.dataDir.appendingPathComponent(isDark ? "mac-bg-dark.jpg" : "mac-bg-light.jpg")
    }

    // Per-tab caches (load once, read from memory across tab switches).
    let downloads = DownloadsStore()
    let best = BestStore()
    let groupsStore = GroupsStore()

    func boot() async {
        Backend.shared.start()
        ready = await API.waitForBackend()
        await refresh()
    }

    // Photo-group membership: { groupName: [assetIds] }.
    var photoGroups: [String: [String]] = [:]

    func refresh() async {
        async let s = try? await API.stats()
        async let l = try? await API.stockList()
        async let g = try? await API.photoGroups()
        if let s = await s { stats = s }
        if let l = await l { stockList = ["All"] + l }
        if let g = await g { photoGroups = g }
    }

    /// Group names that contain a given asset id.
    func groups(for assetID: String) -> [String] {
        photoGroups.filter { $0.value.contains(assetID) }.keys.sorted()
    }

    var allGroupNames: [String] { photoGroups.keys.sorted() }

    /// Add/remove an asset from a group, then persist the whole map.
    func toggleMember(group: String, assetID: String) async {
        var map = photoGroups
        var ids = map[group] ?? []
        if let idx = ids.firstIndex(of: assetID) { ids.remove(at: idx) }
        else { ids.append(assetID) }
        map[group] = ids
        photoGroups = map
        try? await API.savePhotoGroups(map)
    }

    func createGroup(_ name: String, with assetID: String) async {
        let n = name.trimmingCharacters(in: .whitespaces)
        guard !n.isEmpty, photoGroups[n] == nil else { return }
        var map = photoGroups
        map[n] = [assetID]
        photoGroups = map
        try? await API.savePhotoGroups(map)
    }

    func renameGroup(_ old: String, to new: String) async {
        _ = try? await API.renameGroup(old: old, new: new)
        await reloadGroups()
    }
    func deleteGroup(_ name: String) async {
        _ = try? await API.deleteGroup(name)
        await reloadGroups()
    }
    func mergeGroup(source: String, into target: String) async {
        _ = try? await API.mergeGroup(source: source, target: target)
        await reloadGroups()
    }

    private func reloadGroups() async {
        if let g = try? await API.photoGroups() { photoGroups = g }
        groupsStore.loaded = false
        await groupsStore.ensure()
    }

    func statBlock(_ p: Period) -> StatBlock {
        let f = downloadsStock
        if f == "All" {
            switch p {
            case .today: return stats.today
            case .week:  return stats.week
            case .month: return stats.month
            case .year:  return stats.year
            case .all:   return stats.all
            }
        }
        if p == .all {
            if let e = stats.by_stock.first(where: { $0.stock == f }) {
                return StatBlock(total: e.total, count: e.count, delta: 0)
            }
            return StatBlock()
        }
        return StatBlock() // per-period per-stock omitted in this MVP slice
    }
}
