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
    let surfaces = SurfaceTheme()

    /// Path of the user-imported background for the current theme (if any).
    var backgroundURL: URL {
        Backend.shared.dataDir.appendingPathComponent(isDark ? "mac-bg-dark.jpg" : "mac-bg-light.jpg")
    }

    // Per-tab caches (load once, read from memory across tab switches).
    let downloads = DownloadsStore()
    let best = BestStore()
    let groupsStore = GroupsStore()

    // Keys of sales added in the last sync — used for blue highlight.
    // Format: "asset_id|date|stock|price"
    var newSaleKeys: Set<String> = []

    // Incremented every time a sync finishes — all tabs watch this to auto-reload.
    var syncTick: Int = 0

    func notifySyncDone() async {
        await fetchNewKeys()
        await refresh()
        downloads.caches = [:]
        best.caches = [:]
        groupsStore.loaded = false
        syncTick += 1
    }

    // Stock colors loaded from /api/stock-colors (persisted in recipes/stock_colors.json)
    var stockColors: [String: Color] = Theme.stockColors

    func color(for stock: String) -> Color { stockColors[stock] ?? .gray }

    // Live theme colors — read by views so Materials Lab text/accent sliders apply everywhere.
    var t1: Color { surfaces.t1(isDark: isDark) }
    var t2: Color { surfaces.t2(isDark: isDark) }
    var t3: Color { surfaces.t3(isDark: isDark) }
    var accent: Color { surfaces.accent }

    func loadStockColors() async {
        guard let url = URL(string: API.base + "/api/stock-colors"),
              let (data, _) = try? await URLSession.shared.data(from: url),
              let raw = try? JSONDecoder().decode([String: String].self, from: data) else { return }
        var map: [String: Color] = [:]
        for (k, v) in raw { if let c = Color(hex: v) { map[k] = c } }
        if !map.isEmpty { stockColors = map }
    }

    func saveStockColor(_ stock: String, _ color: Color) async {
        var current = stockColors
        current[stock] = color
        stockColors = current
        let hex = color.hexString
        guard let url = URL(string: API.base + "/api/stock-colors") else { return }
        var req = URLRequest(url: url); req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONEncoder().encode([stock: hex])
        _ = try? await URLSession.shared.data(for: req)
    }

    func fetchNewKeys() async {
        guard let url = URL(string: API.base + "/api/sync/recent-keys") else { return }
        guard let (data, _) = try? await URLSession.shared.data(from: url),
              let keys = try? JSONDecoder().decode([String].self, from: data) else { return }
        newSaleKeys = Set(keys)
    }

    func clearNewKeys() { newSaleKeys = [] }

    func boot() async {
        surfaces.load()
        Backend.shared.start()
        ready = await API.waitForBackend()
        await refresh()
        await loadStockColors()
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

    /// Active stock filter — Downloads stock on tab 0, BestSellers stock on tab 1.
    var activeStock: String {
        switch activeTab {
        case 1: return best.stock
        default: return downloadsStock
        }
    }

    func statBlock(_ p: Period) -> StatBlock {
        let f = activeStock
        let base: StatBlock = {
            switch p {
            case .today: return stats.today
            case .week:  return stats.week
            case .month: return stats.month
            case .year:  return stats.year
            case .all:   return stats.all
            }
        }()
        guard f != "All" else { return base }
        // Per-period per-stock data lives in base.by_stock
        if let bs = base.by_stock, let s = bs[f] {
            return StatBlock(total: s.total, count: s.count, delta: 0)
        }
        // Fallback for .all period via top-level by_stock list
        if p == .all, let e = stats.by_stock.first(where: { $0.stock == f }) {
            return StatBlock(total: e.total, count: e.count, delta: 0)
        }
        return StatBlock()
    }
}
