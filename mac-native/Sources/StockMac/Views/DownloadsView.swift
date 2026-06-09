import SwiftUI

struct DownloadsView: View {
    @Environment(AppModel.self) private var model
    @State private var popup: Sale?
    @State private var assign: AssignTarget?
    private var cardW: CGFloat { model.surfaces.size("cardW", 168) }
    private var cols: [GridItem] { [GridItem(.adaptive(minimum: cardW, maximum: cardW), spacing: model.surfaces.cardSpacing)] }
    private var panelH: CGFloat { model.surfaces.panelH }

    private var store: DownloadsStore { model.downloads }

    var body: some View {
        PanelScroll(panelHeight: panelH) {
            filters
        } content: {
            LazyVGrid(columns: cols, spacing: model.surfaces.cardSpacing) {
                ForEach(store.items) { sale in
                    SaleCard(sale: sale)
                        .onTapGesture { popup = sale }
                        .contextMenu {
                            Button("Open") { popup = sale }
                            Button("Add to group…") { assign = AssignTarget(id: sale.asset_id) }
                        }
                        // infinite scroll: load next page as the last card appears
                        .onAppear {
                            if sale.identity == store.items.last?.identity {
                                Task { await store.loadPage(period: model.period, stock: model.downloadsStock) }
                            }
                        }
                }
            }
            if store.loading { ProgressView().padding() }
        }
        .task(id: model.period) { await store.select(period: model.period, stock: model.downloadsStock) }
        .popupHost(item: $popup) { s in
            PhotoPopup(assetID: s.asset_id, thumb: s.thumb_url, byStock: s.by_stock ?? [:],
                       onClose: { popup = nil }).environment(model)
        }
        .popupHost(item: $assign) { t in
            GroupAssignPopup(assetID: t.id, onClose: { assign = nil }).environment(model)
        }
    }

    @State private var syncing = false
    @State private var spinDeg: Double = 0

    private var filters: some View {
        HStack(spacing: 6) {
            Text("Stock:").font(.system(size: 12, weight: .semibold)).foregroundStyle(model.t3)
            SlidingPills(options: model.stockList, selected: model.downloadsStock) { s in
                model.downloadsStock = s
                Task { await store.select(period: model.period, stock: s) }
            }
            Spacer()
            Button {
                guard !syncing else { return }
                Task { await doSync() }
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: "arrow.triangle.2.circlepath")
                        .font(.system(size: 13, weight: .semibold))
                        .rotationEffect(.degrees(spinDeg))
                    Text(syncing ? "Syncing…" : "Sync")
                        .font(.system(size: 13, weight: .semibold))
                }
                .foregroundStyle(.white)
            }
            .buttonStyle(.pill(.primary))
            .disabled(syncing)
        }
        .padding(10)
        .glassCard()
        .frame(height: panelH)
    }

    private func doSync() async {
        syncing = true
        withAnimation(.linear(duration: 1).repeatForever(autoreverses: false)) { spinDeg = 360 }
        var req = URLRequest(url: URL(string: API.base + "/api/sync/start")!)
        req.httpMethod = "POST"; req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = Data("{}".utf8)
        _ = try? await URLSession.shared.data(for: req)
        // wait for SSE done
        if let url = URL(string: API.base + "/api/sync/stream"),
           let (stream, _) = try? await URLSession.shared.bytes(from: url) {
            do {
                for try await line in stream.lines {
                    if line.hasPrefix("data:") && line.dropFirst(5).trimmingCharacters(in: .whitespaces) == "done" { break }
                }
            } catch {}
        }
        withAnimation { spinDeg = 0 }
        syncing = false
        // notifySyncDone fetches newSaleKeys (blue highlight), refreshes stats and
        // CLEARS the downloads cache so the reload below pulls the new sales. Without
        // this the just-synced sales never turned blue — the bug that kept recurring.
        await model.notifySyncDone()
        await store.select(period: model.period, stock: model.downloadsStock)
    }
}

struct SaleCard: View {
    let sale: Sale
    @Environment(AppModel.self) private var model

    private var groupLabel: String? { model.groups(for: sale.asset_id).first }

    /// Blue highlight for freshly-synced sales. sale.saleKey matches the backend
    /// _session_new_keys format; model.newSaleKeys is fetched from /api/sync/recent-keys.
    private var isNew: Bool { model.newSaleKeys.contains(sale.saleKey) }
    @State private var pulse = false

    var body: some View {
        let s = model.surfaces
        let w = s.size("cardW", 168)
        let imgH = s.size("cardImgH", 112)
        VStack(alignment: .leading, spacing: 0) {
            ZStack(alignment: .topLeading) {
                CachedThumb(assetID: sale.asset_id, fallback: sale.thumb_url)
                    .frame(width: w, height: imgH)
                    .clipped()
                if sale.isFirstSale {
                    Text("NEW")
                        .font(.system(size: 8, weight: .bold))
                        .padding(.horizontal, 5).padding(.vertical, 2)
                        .background(model.accent, in: .rect(bottomTrailingRadius: 6))
                        .foregroundStyle(.white)
                }
            }
            VStack(alignment: .leading, spacing: 1) {
                Text(sale.price.money)
                    .font(.system(size: 15, weight: .bold))
                    .foregroundStyle(isNew ? model.accent : model.t1)
                    .contentTransition(.numericText())
                HStack(spacing: 4) {
                    Circle().fill(model.color(for: sale.stock)).frame(width: 6, height: 6)
                    Text("\(sale.stock) · \(String(sale.date.suffix(5)))")
                        .font(.system(size: 10))
                        .foregroundStyle(model.t2)
                        .lineLimit(1)
                }
                if let g = groupLabel {
                    Text("📁 \(g)").font(.system(size: 8))
                        .foregroundStyle(model.t3)
                        .lineLimit(1)
                }
            }
            .padding(.horizontal, 9).padding(.top, 7).padding(.bottom, 9)
            .frame(maxWidth: .infinity, alignment: .leading)
            .surfaceMat(s.mat(.cardInfoBg, isDark: model.isDark),
                        interactive: false, radius: s.cardInfoRadius)
        }
        .frame(width: w)
        .clipShape(RoundedRectangle(cornerRadius: s.cardRadius))
        .cardSurface()
        // One-shot "pop" when a freshly-synced (blue) sale appears.
        .scaleEffect(pulse ? 1.05 : 1.0)
        .onAppear {
            guard isNew else { return }
            withAnimation(.spring(response: 0.22, dampingFraction: 0.5)) { pulse = true }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.22) {
                withAnimation(.spring(response: 0.4, dampingFraction: 0.6)) { pulse = false }
            }
        }
        // Fade + scale in as cards scroll into view.
        .scrollTransition { content, phase in
            content
                .opacity(phase.isIdentity ? 1 : 0.0)
                .scaleEffect(phase.isIdentity ? 1 : 0.94)
        }
    }
}
