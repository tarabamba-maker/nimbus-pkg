import Foundation

/// Thin async client over the local Flask backend (same one Tauri uses).
enum API {
    static let base = "http://localhost:8000"

    static func imageURL(_ assetID: String) -> URL? {
        URL(string: "\(base)/img/cache/\(assetID)")
    }

    private static func get<T: Decodable>(_ path: String) async throws -> T {
        guard let url = URL(string: base + path) else { throw URLError(.badURL) }
        var req = URLRequest(url: url)
        req.timeoutInterval = 20
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
        return try JSONDecoder().decode(T.self, from: data)
    }

    static func stats() async throws -> Stats {
        try await get("/api/stats")
    }

    static func stockList() async throws -> [String] {
        try await get("/api/stock-list")
    }

    static func feed(period: Period, stock: String, page: Int, perPage: Int = 60) async throws -> Feed {
        let q = "period=\(enc(period.rawValue))&stock=\(enc(stock))&page=\(page)&per_page=\(perPage)"
        return try await get("/api/feed?\(q)")
    }

    static func sales(period: Period, stock: String, sort: String, page: Int,
                      perPage: Int = 60, query: String = "") async throws -> SalesResponse {
        var q = "period=\(enc(period.rawValue))&stock=\(enc(stock))&sort=\(sort)&page=\(page)&per_page=\(perPage)"
        if !query.isEmpty { q += "&q=\(enc(query))" }
        return try await get("/api/sales?\(q)")
    }

    // MARK: photo groups

    static func photoGroups() async throws -> [String: [String]] {
        try await get("/api/photo-groups")
    }

    @discardableResult
    static func savePhotoGroups(_ map: [String: [String]]) async throws -> Bool {
        try await postJSON("/api/photo-groups", body: map)
    }

    @discardableResult
    static func renameGroup(old: String, new: String) async throws -> Bool {
        try await postJSON("/api/photo-groups/rename", body: ["old_name": old, "new_name": new])
    }

    @discardableResult
    static func mergeGroup(source: String, target: String) async throws -> Bool {
        try await postJSON("/api/photo-groups/merge", body: ["source": source, "target": target])
    }

    @discardableResult
    static func matchOverride(action: String, primary: String, assetID: String) async throws -> Bool {
        try await postJSON("/api/match-override", body: ["action": action, "primary": primary, "asset_id": assetID])
    }

    @discardableResult
    static func deleteGroup(_ name: String) async throws -> Bool {
        let enc = name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? name
        var req = URLRequest(url: URL(string: base + "/api/photo-groups/" + enc)!)
        req.httpMethod = "DELETE"
        let (_, resp) = try await URLSession.shared.data(for: req)
        return (resp as? HTTPURLResponse).map { (200..<300).contains($0.statusCode) } ?? false
    }

    @discardableResult
    private static func postJSON<B: Encodable>(_ path: String, body: B) async throws -> Bool {
        var req = URLRequest(url: URL(string: base + path)!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONEncoder().encode(body)
        let (_, resp) = try await URLSession.shared.data(for: req)
        return (resp as? HTTPURLResponse).map { (200..<300).contains($0.statusCode) } ?? false
    }

    /// Waits until the backend answers /api/stats (used right after we spawn it).
    static func waitForBackend(timeout: TimeInterval = 30) async -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if (try? await stats()) != nil { return true }
            try? await Task.sleep(nanoseconds: 400_000_000)
        }
        return false
    }

    private static func enc(_ s: String) -> String {
        s.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? s
    }
}
