import Foundation

// MARK: - Stats (/api/stats)

struct StatBlock: Codable, Equatable {
    var total: Double = 0
    var count: Int = 0
    var delta: Double? = 0
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
    var by_stock: [String: SaleStockStat]?

    /// Stable identity even when the API omits a numeric id.
    var identity: String { "\(asset_id)|\(date)|\(stock)|\(price)" }

    var isFirstSale: Bool {
        guard let bs = by_stock else { return false }
        return bs.values.reduce(0) { $0 + $1.count } == 1
    }
}

struct SaleStockStat: Codable, Equatable {
    var count: Int = 0
    var total: Double = 0
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
