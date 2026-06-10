import SwiftUI
import AppKit

/// Small in-memory thumbnail cache backed by the backend's /img/cache route,
/// falling back to the remote stock URL if the local cache misses.
actor ThumbCache {
    static let shared = ThumbCache()
    private var cache: [String: NSImage] = [:]

    func image(assetID: String, fallback: String?) async -> NSImage? {
        if let hit = cache[assetID] { return hit }
        if let url = API.imageURL(assetID), let img = await fetch(url), img.size.width > 2 {
            cache[assetID] = img; return img
        }
        if let f = fallback, let url = URL(string: f), let img = await fetch(url) {
            cache[assetID] = img; return img
        }
        return nil
    }

    /// Drop a cached entry so the next fetch re-pulls (after a manual thumb upload).
    func invalidate(_ assetID: String) { cache[assetID] = nil }

    private func fetch(_ url: URL) async -> NSImage? {
        guard let (data, _) = try? await URLSession.shared.data(from: url) else { return nil }
        return NSImage(data: data)
    }
}

struct CachedThumb: View {
    let assetID: String
    let fallback: String?
    @State private var image: NSImage?

    var body: some View {
        Group {
            if let image {
                Image(nsImage: image).resizable().aspectRatio(contentMode: .fill)
            } else {
                Rectangle().fill(.quaternary)
                    .overlay { Image(systemName: "photo").foregroundStyle(Theme.t3) }
            }
        }
        .task(id: assetID) {
            image = await ThumbCache.shared.image(assetID: assetID, fallback: fallback)
        }
        // Refresh when this asset's thumbnail is manually replaced.
        .onReceive(NotificationCenter.default.publisher(for: .thumbUpdated)) { note in
            guard (note.object as? String) == assetID else { return }
            Task {
                await ThumbCache.shared.invalidate(assetID)
                image = await ThumbCache.shared.image(assetID: assetID, fallback: fallback)
            }
        }
    }
}

extension Notification.Name {
    static let thumbUpdated = Notification.Name("thumbUpdated")
}
