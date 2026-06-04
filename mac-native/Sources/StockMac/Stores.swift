import SwiftUI
import Observation

/// Per-tab caches keyed by the active filter combination, so switching back to a
/// filter you already viewed reads straight from memory (no refetch).

struct PageCache<T> {
    var items: [T] = []
    var page = 1
    var end = false
}

@MainActor @Observable
final class DownloadsStore {
    var caches: [String: PageCache<Sale>] = [:]
    var current = "All-time|All"
    var loading = false

    var items: [Sale] { caches[current]?.items ?? [] }

    private func key(_ period: Period, _ stock: String) -> String { "\(period.rawValue)|\(stock)" }

    /// Switch to a filter; fetch only if it isn't cached yet.
    func select(period: Period, stock: String) async {
        current = key(period, stock)
        if caches[current] == nil { await loadPage(period: period, stock: stock) }
    }

    func loadPage(period: Period, stock: String) async {
        let k = key(period, stock)
        var c = caches[k] ?? PageCache<Sale>()
        guard !loading, !c.end else { return }
        loading = true; defer { loading = false }
        if let feed = try? await API.feed(period: period, stock: stock, page: c.page) {
            if feed.items.isEmpty { c.end = true }
            else { c.items.append(contentsOf: feed.items); c.page += 1 }
            caches[k] = c
        }
    }
}

@MainActor @Observable
final class BestStore {
    var caches: [String: PageCache<TopPhoto>] = [:]
    var loading = false
    var sort = "earnings"
    var stock = "All"
    var query = ""

    private var key: String { "\(sort)|\(stock)|\(query)" }
    var items: [TopPhoto] { caches[key]?.items ?? [] }

    /// Fetch current filter if not cached.
    func ensure() async {
        if caches[key] == nil { await loadPage() }
    }

    /// Force a fresh fetch for the current filter (used after editing query).
    func reloadCurrent() async {
        caches[key] = nil
        await loadPage()
    }

    func loadPage() async {
        let k = key
        var c = caches[k] ?? PageCache<TopPhoto>()
        guard !loading, !c.end else { return }
        loading = true; defer { loading = false }
        if let r = try? await API.sales(period: .all, stock: stock, sort: sort, page: c.page, query: query) {
            if r.items.isEmpty { c.end = true }
            else { c.items.append(contentsOf: r.items); c.page += 1 }
            caches[k] = c
        }
    }
}

@MainActor @Observable
final class GroupsStore {
    var groups: [PhotoGroup] = []
    var loading = false
    var loaded = false

    func ensure() async { if !loaded { await load() } }

    func load() async {
        loading = true; defer { loading = false }
        if let url = URL(string: API.base + "/api/groups"),
           let (data, _) = try? await URLSession.shared.data(from: url),
           let gs = try? JSONDecoder().decode([PhotoGroup].self, from: data) {
            groups = gs; loaded = true
        }
    }
}
