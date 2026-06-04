import SwiftUI

struct DownloadsView: View {
    @Environment(AppModel.self) private var model
    @State private var popup: Sale?
    @State private var assign: AssignTarget?
    private let cols = [GridItem(.adaptive(minimum: 168, maximum: 168), spacing: 10)]
    private let panelH: CGFloat = 52

    private var store: DownloadsStore { model.downloads }

    var body: some View {
        PanelScroll(panelHeight: panelH) {
            filters
        } content: {
            LazyVGrid(columns: cols, spacing: 10) {
                ForEach(store.items) { sale in
                    SaleCard(sale: sale)
                        .onTapGesture { popup = sale }
                        .contextMenu {
                            Button("Open") { popup = sale }
                            Button("Add to group…") { assign = AssignTarget(id: sale.asset_id) }
                        }
                }
            }
            // bottom sentinel: keeps paging even when filters hide trailing cards
            Color.clear.frame(height: 1)
                .onAppear { Task { await store.loadPage(period: model.period, stock: model.downloadsStock) } }
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

    private var filters: some View {
        HStack(spacing: 6) {
            Text("Stock:").font(.system(size: 12, weight: .semibold)).foregroundStyle(Theme.t3)
            SlidingPills(options: model.stockList, selected: model.downloadsStock) { s in
                model.downloadsStock = s
                Task { await store.select(period: model.period, stock: s) }
            }
            Spacer()
        }
        .padding(10)
        .glassCard(16)
        .frame(height: panelH)
    }
}

struct SaleCard: View {
    let sale: Sale
    @Environment(AppModel.self) private var model

    private var groupLabel: String? { model.groups(for: sale.asset_id).first }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ZStack(alignment: .topLeading) {
                CachedThumb(assetID: sale.asset_id, fallback: sale.thumb_url)
                    .frame(width: 168, height: 112)
                    .clipped()
                if sale.isFirstSale {
                    Text("NEW")
                        .font(.system(size: 8, weight: .bold))
                        .padding(.horizontal, 5).padding(.vertical, 2)
                        .background(Theme.accent, in: .rect(bottomTrailingRadius: 6))
                        .foregroundStyle(.white)
                }
            }
            VStack(alignment: .leading, spacing: 1) {
                Text(sale.price.money)
                    .font(.system(size: 15, weight: .bold))
                HStack(spacing: 4) {
                    Circle().fill(Theme.color(for: sale.stock)).frame(width: 6, height: 6)
                    Text("\(sale.stock) · \(String(sale.date.suffix(5)))")
                        .font(.system(size: 10)).foregroundStyle(Theme.t2).lineLimit(1)
                }
                if let g = groupLabel {
                    Text("📁 \(g)").font(.system(size: 8)).foregroundStyle(Theme.t3).lineLimit(1)
                }
            }
            .padding(.horizontal, 9).padding(.top, 7).padding(.bottom, 9)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(width: 168)
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .cardSurface(14)
    }
}
