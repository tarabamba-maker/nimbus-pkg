import Foundation

// MARK: - Stats (/api/stats)

struct StatBlock: Codable, Equatable {
    var total: Double = 0
    var count: Int = 0
    var delta: Double? = 0
    var new_total: Double? = nil   // today only: money added this sync (green badge)
    var new_count: Int? = nil
    var by_stock: [String: SaleStockStat]? = nil
}

struct StockTotal: Codable, Identifiable, Equatable {
    var stock: String
    var total: Double
    var count: Int
    var id: String { stock }
}

struct Stats: Codable, Equatable {
    var today = StatBlock()
    var week  = StatBlock()
    var month = StatBlock()
    var year  = StatBlock()
    var all   = StatBlock()
    var by_stock: [StockTotal] = []
}

// MARK: - Feed (/api/feed)

struct Sale: Codable, Identifiable, Equatable {
    var id: Int?
    var asset_id: String
    var date: String
    var price: Double
    var stock: String
    var thumb_url: String?
    var thumb_aid: String?
    var by_stock: [String: SaleStockStat]?

    /// Stable identity even when the API omits a numeric id.
    var identity: String { "\(asset_id)|\(date)|\(stock)|\(price)" }

    var isFirstSale: Bool {
        guard let bs = by_stock else { return false }
        return bs.values.reduce(0) { $0 + $1.count } == 1
    }

    /// Key that matches backend `_session_new_keys` format.
    var saleKey: String {
        let day = date.count >= 10 ? String(date.prefix(10)) : date
        return "\(asset_id)|\(day)|\(stock)|\(String(format: "%.2f", price))"
    }
}

struct SaleStockStat: Codable, Equatable {
    var count: Int = 0
    var total: Double = 0
}

/// Payload for the global photo-detail popup (presented at RootView).
struct PhotoPopupData: Identifiable, Equatable {
    let assetID: String
    let thumb: String?
    let byStock: [String: SaleStockStat]
    var id: String { assetID }
}

struct Feed: Codable {
    var items: [Sale] = []
    var total: Int?
}

// MARK: - Best Sellers (/api/sales)

struct TopPhoto: Codable, Identifiable, Equatable {
    var asset_id: String
    var total: Double
    var count: Int
    var stock: String
    var thumb_url: String?
    var thumb_aid: String?
    var merged: Bool?
    var by_stock: [String: SaleStockStat]?
    var id: String { asset_id }

    var stocks: [String] {
        (by_stock ?? [:]).filter { $0.value.count > 0 }.keys.sorted()
    }
}

struct SalesResponse: Codable {
    var items: [TopPhoto] = []
    var total: Int?
}

// MARK: - Analytics (/api/analytics)

struct AnalyticsPoint: Codable, Identifiable, Equatable {
    var bucket: String
    var total: Double
    var count: Int
    var by_stock: [String: SaleStockStat]?
    var id: String { bucket }
}

struct AnalyticsStock: Codable, Identifiable, Equatable {
    var stock: String
    var total: Double
    var count: Int
    var id: String { stock }
}

struct Analytics: Codable, Equatable {
    var granularity: String = "day"
    var start: String = ""
    var end: String = ""
    var total: Double = 0
    var count: Int = 0
    var timeline: [AnalyticsPoint] = []
    var by_stock: [AnalyticsStock] = []
}

// MARK: - Periods

enum Period: String, CaseIterable, Identifiable {
    case today = "Today", week = "Week", month = "Month", year = "Year", all = "All-time"
    var id: String { rawValue }
    var short: String {
        switch self {
        case .today: return "TODAY"
        case .week:  return "WEEK"
        case .month: return "MONTH"
        case .year:  return "YEAR"
        case .all:   return "ALL-TIME"
        }
    }
}
