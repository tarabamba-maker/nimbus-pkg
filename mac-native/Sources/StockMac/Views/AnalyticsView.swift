import SwiftUI
import Charts

/// Analytics tab — bar + line timeline and a per-stock donut/bar breakdown.
///
/// Reacts to the SAME global controls as every other tab:
///   • the Today/Week/Month/Year/All stat blocks (`model.period`) — when the
///     range mode is "Period",
///   • the stock filter pills,
///   • an Earnings/Sales metric toggle (same look as Best Sellers' sort).
/// Range modes:
///   • Period  — follows the global period blocks,
///   • Months  — a 12-month window with ◀/▶ paging,
///   • Years   — a 12-year window with ◀/▶ paging,
///   • Custom  — two date pickers (only in Period mode).
/// Hovering a bar / point shows a tooltip with the exact figure.
struct AnalyticsView: View {
    @Environment(AppModel.self) private var model

    @State private var data = Analytics()
    @State private var loading = false
    @State private var metric = "Earnings"        // Earnings | Sales
    @State private var stock = "All"
    @State private var range = "Period"            // Period | Months | Years
    @State private var offset = 0                  // window offset (months/years), ≤ 0
    @State private var customRange = false
    @State private var startDate = Calendar.current.date(byAdding: .month, value: -1, to: Date()) ?? Date()
    @State private var endDate = Date()

    // Hover state for the tooltips.
    @State private var hoverPoint: AnalyticsPoint?         // timeline bucket under cursor
    @State private var hoverSeg: (stock: String, value: Double)?  // specific stacked segment
    @State private var hoverPLoc: CGPoint = .zero
    @State private var hoverStock: AnalyticsStock?         // horizontal stock bar
    @State private var hoverSLoc: CGPoint = .zero
    @State private var hoverDonut: AnalyticsStock?         // donut sector
    @State private var hoverDLoc: CGPoint = .zero

    private var panelH: CGFloat { model.surfaces.panelH }
    private var byCount: Bool { metric == "Sales" }

    var body: some View {
        PanelScroll(panelHeight: panelH) {
            controls
        } content: {
            VStack(alignment: .leading, spacing: 16) {
                if loading && data.timeline.isEmpty {
                    ProgressView().frame(maxWidth: .infinity).padding(.top, 40)
                } else if data.timeline.isEmpty {
                    Text("No data for this range")
                        .foregroundStyle(model.t2)
                        .frame(maxWidth: .infinity).padding(.top, 40)
                } else {
                    summaryRow
                    timelineCard
                    HStack(alignment: .top, spacing: 16) {
                        donutCard
                        stockBarsCard
                    }
                }
            }
            .padding(.horizontal, 4)
            .padding(.bottom, 24)
        }
        .task(id: reloadKey) { await reload() }
    }

    // Recompute whenever any input that affects the query changes.
    private var reloadKey: String {
        "\(model.period.rawValue)|\(stock)|\(range)|\(offset)|\(customRange)|\(fmt(startDate))|\(fmt(endDate))"
    }

    private func reload() async {
        loading = true
        defer { loading = false }
        let result: Analytics?
        if range != "Period", let (s, e) = browseWindow() {
            result = try? await API.analytics(period: model.period, stock: stock, start: s, end: e)
        } else if customRange {
            result = try? await API.analytics(period: model.period, stock: stock,
                                              start: fmt(startDate), end: fmt(endDate))
        } else {
            result = try? await API.analytics(period: model.period, stock: stock)
        }
        if let r = result { data = r }
    }

    /// Start/end strings for the Months/Years browse window (most-recent = offset 0).
    private func browseWindow() -> (String, String)? {
        let cal = Calendar.current
        let now = Date()
        if range == "Months" {
            guard let anchor = cal.date(byAdding: .month, value: offset, to: now),
                  let endInt = cal.dateInterval(of: .month, for: anchor),
                  let startAnchor = cal.date(byAdding: .month, value: -11, to: anchor),
                  let startInt = cal.dateInterval(of: .month, for: startAnchor) else { return nil }
            return (fmt(startInt.start), fmt(endInt.end.addingTimeInterval(-1)))
        } else { // Years
            guard let anchor = cal.date(byAdding: .year, value: offset, to: now) else { return nil }
            let y = cal.component(.year, from: anchor)
            return ("\(y - 11)-01-01", "\(y)-12-31")
        }
    }

    // MARK: controls

    private var controls: some View {
        HStack(spacing: 8) {
            SortSegmented(
                options: ["Earnings", "Sales"],
                selected: metric, asc: false,
                onSelect: { metric = $0 }, onToggleDir: {})
                .offset(x: model.surfaces.pillOffsetX, y: model.surfaces.pillOffsetY)
            Divider().frame(height: 18)
            SlidingPills(options: model.stockList, selected: stock) { stock = $0 }
            Spacer()
            // Range mode: follow period, or page months/years.
            SlidingPills(options: ["Period", "Months", "Years"], selected: range) { r in
                range = r; offset = 0
                if r != "Period" { customRange = false }
            }
            if range != "Period" {
                HStack(spacing: 4) {
                    Button { offset -= 12 } label: { Image(systemName: "chevron.left") }
                        .buttonStyle(.plain).glassPill()
                    Button { offset = min(0, offset + 12) } label: { Image(systemName: "chevron.right") }
                        .buttonStyle(.plain).glassPill().disabled(offset >= 0).opacity(offset >= 0 ? 0.4 : 1)
                }
            } else {
                Button { customRange.toggle() } label: {
                    Label("Custom", systemImage: "calendar")
                        .font(.system(size: 12, weight: .semibold))
                        .padding(.horizontal, 12).padding(.vertical, 5)
                        .foregroundStyle(customRange ? .white : .secondary)
                }
                .buttonStyle(.plain).glassPill(active: customRange)
                if customRange {
                    DatePicker("", selection: $startDate, displayedComponents: .date)
                        .labelsHidden().datePickerStyle(.field)
                    Text("→").foregroundStyle(model.t3)
                    DatePicker("", selection: $endDate, displayedComponents: .date)
                        .labelsHidden().datePickerStyle(.field)
                }
            }
        }
        .padding(10)
        .glassCard()
        .frame(height: panelH)
    }

    // MARK: summary

    private var summaryRow: some View {
        HStack(spacing: 20) {
            stat("Earnings", data.total.money)
            stat("Sales", data.count.formatted())
            stat("Range", "\(data.start) → \(data.end)")
            Spacer()
        }
        .padding(.horizontal, 4)
    }

    private func stat(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label.uppercased()).font(.system(size: 9, weight: .semibold)).foregroundStyle(model.t3)
            Text(value).font(.system(size: 16, weight: .bold)).foregroundStyle(model.t1)
                .contentTransition(.numericText())
        }
    }

    // MARK: timeline (bars + line) with hover tooltip

    /// One stacked segment: a stock's contribution to a time bucket.
    private struct StackSeg: Identifiable {
        let bucket: String, stock: String, value: Double
        var id: String { bucket + "|" + stock }
    }

    private var stackSeries: [StackSeg] {
        data.timeline.flatMap { p in
            orderedSegs(p).map { StackSeg(bucket: label(p.bucket), stock: $0.0, value: $0.1) }
        }
    }

    // Stock → colour domain/range so the stacked bars match the donut & legend.
    private var stockDomain: [String] { data.by_stock.map { $0.stock } }

    /// A bucket's stocks in stacking order (= colour domain order, bottom → top).
    private func orderedSegs(_ p: AnalyticsPoint) -> [(String, Double)] {
        let bs = p.by_stock ?? [:]
        return stockDomain.compactMap { s in
            bs[s].map { (s, byCount ? Double($0.count) : $0.total) }
        }
    }

    /// Which stacked segment sits at the given y data-value within a bucket.
    private func segmentAt(_ p: AnalyticsPoint, yValue: Double?) -> (stock: String, value: Double)? {
        guard let yv = yValue, yv >= 0 else { return nil }
        var cum = 0.0
        for (s, v) in orderedSegs(p) {
            if yv >= cum && yv < cum + v { return (s, v) }
            cum += v
        }
        return nil
    }

    private var timelineCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(byCount ? "Sales over time" : "Earnings over time")
                .font(.system(size: 13, weight: .semibold)).foregroundStyle(model.t1)
            Chart {
                ForEach(stackSeries) { seg in
                    BarMark(x: .value("Period", seg.bucket), y: .value(metric, seg.value))
                        .foregroundStyle(by: .value("Stock", seg.stock))
                        .cornerRadius(2)
                }
                ForEach(data.timeline) { p in
                    let y = byCount ? Double(p.count) : p.total
                    LineMark(x: .value("Period", label(p.bucket)), y: .value(metric, y))
                        .foregroundStyle(model.t1.opacity(0.55))
                        .interpolationMethod(.catmullRom)
                        .lineStyle(StrokeStyle(lineWidth: 1.5, dash: [4, 3]))
                }
            }
            .chartForegroundStyleScale(domain: stockDomain,
                                       range: stockDomain.map { AnyShapeStyle(glassBar(model.color(for: $0))) })
            .chartLegend(.hidden)
            .chartPlotStyle { $0.background { plotGlass } }
            .chartXAxis {
                AxisMarks(values: .automatic(desiredCount: 8)) { _ in
                    AxisGridLine(); AxisTick(); AxisValueLabel().font(.system(size: 9))
                }
            }
            .chartOverlay { proxy in
                GeometryReader { geo in
                    Rectangle().fill(.clear).contentShape(Rectangle())
                        .onContinuousHover { phase in
                            if case .active(let loc) = phase, let anchor = proxy.plotFrame {
                                let origin = geo[anchor].origin
                                let yv: Double? = proxy.value(atY: loc.y - origin.y)
                                if let cat: String = proxy.value(atX: loc.x - origin.x),
                                   let pt = data.timeline.first(where: { label($0.bucket) == cat }) {
                                    hoverPoint = pt; hoverPLoc = loc
                                    hoverSeg = segmentAt(pt, yValue: yv)
                                }
                            } else { hoverPoint = nil; hoverSeg = nil }
                        }
                }
            }
            .overlay(alignment: .topLeading) {
                if let pt = hoverPoint {
                    timelineTooltip(pt)
                        .offset(x: max(0, hoverPLoc.x - 36), y: max(0, hoverPLoc.y - 56))
                }
            }
            .frame(height: 240)
        }
        .padding(14)
        .glassCard()
    }

    // MARK: per-stock donut

    private var donutCard: some View {
        let items = data.by_stock.filter {
            let v = byCount ? Double($0.count) : $0.total
            return v > 0 && v.isFinite
        }
        let total = items.reduce(0.0) { $0 + (byCount ? Double($1.count) : $1.total) }
        return VStack(alignment: .leading, spacing: 10) {
            Text("Share by stock").font(.system(size: 13, weight: .semibold)).foregroundStyle(model.t1)
            if items.isEmpty {
                Text("—").foregroundStyle(model.t2).frame(height: 220)
            } else {
                // Swift Charts' SectorMark hard-crashes (acos → NaN → EXC_BREAKPOINT) when
                // rendering pies on this macOS 26 / Charts build — on any slice count, during
                // a layout pass triggered by a mouse event. We draw the donut by hand with
                // Canvas arcs instead, so Apple's pie geometry (and its acos) is never invoked.
                GeometryReader { geo in
                    let side = min(geo.size.width, geo.size.height)
                    let c = CGPoint(x: geo.size.width / 2, y: geo.size.height / 2)
                    let rOuter = side / 2
                    let rInner = rOuter * 0.58
                    ZStack {
                        Canvas { ctx, _ in
                            var start = -Double.pi / 2          // 12 o'clock
                            for s in items {
                                let v = byCount ? Double(s.count) : s.total
                                let sweep = total > 0 ? v / total * 2 * .pi : 0
                                let end = start + sweep
                                var p = Path()
                                p.addArc(center: c, radius: rOuter,
                                         startAngle: .radians(start), endAngle: .radians(end), clockwise: false)
                                p.addArc(center: c, radius: rInner,
                                         startAngle: .radians(end), endAngle: .radians(start), clockwise: true)
                                p.closeSubpath()
                                let dim = hoverDonut != nil && hoverDonut?.id != s.id
                                // Glass sectors: semi-transparent so the glass card behind
                                // shows through (matches the tinted-glass pills).
                                ctx.fill(p, with: .color(model.color(for: s.stock).opacity(dim ? 0.28 : 0.80)))
                                start = end
                            }
                            // Specular sheen across the whole ring — the "glass" highlight.
                            var ring = Path()
                            ring.addArc(center: c, radius: rOuter,
                                        startAngle: .radians(0), endAngle: .radians(2 * .pi), clockwise: false)
                            ring.addArc(center: c, radius: rInner,
                                        startAngle: .radians(2 * .pi), endAngle: .radians(0), clockwise: true)
                            ctx.fill(ring, with: .linearGradient(
                                Gradient(colors: [.white.opacity(0.35), .white.opacity(0.02), .white.opacity(0.10)]),
                                startPoint: CGPoint(x: c.x, y: c.y - rOuter),
                                endPoint: CGPoint(x: c.x, y: c.y + rOuter)))
                        }
                        ForEach(items) { s in
                            let v = byCount ? Double(s.count) : s.total
                            let pct = total > 0 ? v / total * 100 : 0
                            if pct >= 4 {
                                let mid = sliceMidAngle(for: s, items: items, total: total)
                                let rr = (rOuter + rInner) / 2
                                Text("\(Int(pct.rounded()))%")
                                    .font(.system(size: 9, weight: .bold)).foregroundStyle(.white)
                                    .position(x: c.x + CGFloat(cos(mid)) * rr,
                                              y: c.y + CGFloat(sin(mid)) * rr)
                            }
                        }
                    }
                    .contentShape(Rectangle())
                    .onContinuousHover { phase in
                        hoverDonut = donutHover(phase, center: c, rOuter: rOuter, rInner: rInner,
                                                items: items, total: total)
                    }
                }
                .overlay(alignment: .topLeading) {
                    if let s = hoverDonut {
                        let v = byCount ? Double(s.count) : s.total
                        tooltip(title: s.stock,
                                value: byCount
                                    ? "\(s.count) sales · \(Int((total > 0 ? v/total*100 : 0).rounded()))%"
                                    : "\(s.total.money) · \(Int((total > 0 ? v/total*100 : 0).rounded()))%")
                            .offset(x: max(0, hoverDLoc.x - 36), y: max(0, hoverDLoc.y - 44))
                    }
                }
                .frame(height: 220)
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(items) { s in
                        HStack(spacing: 6) {
                            Circle().fill(model.color(for: s.stock)).frame(width: 9, height: 9)
                            Text(s.stock).font(.system(size: 10)).foregroundStyle(model.t2)
                            Spacer()
                            Text(byCount ? "\(s.count)" : s.total.money)
                                .font(.system(size: 10, weight: .semibold)).foregroundStyle(model.t1)
                        }
                    }
                }
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassCard()
    }

    // MARK: per-stock horizontal bars with hover tooltip

    private var stockBarsCard: some View {
        let items = data.by_stock.filter { (byCount ? Double($0.count) : $0.total) > 0 }
        return VStack(alignment: .leading, spacing: 10) {
            Text(byCount ? "Sales by stock" : "Earnings by stock")
                .font(.system(size: 13, weight: .semibold)).foregroundStyle(model.t1)
            if items.isEmpty {
                Text("—").foregroundStyle(model.t2).frame(height: 220)
            } else {
                Chart(items) { s in
                    BarMark(
                        x: .value(metric, byCount ? Double(s.count) : s.total),
                        y: .value("Stock", s.stock))
                        .foregroundStyle(glassBarH(model.color(for: s.stock), active: hoverStock?.id == s.id))
                        .cornerRadius(4)
                }
                .chartPlotStyle { $0.background { plotGlass } }
                .chartXAxis { AxisMarks { _ in AxisGridLine() } }
                .chartOverlay { proxy in
                    GeometryReader { geo in
                        Rectangle().fill(.clear).contentShape(Rectangle())
                            .onContinuousHover { phase in
                                if case .active(let loc) = phase, let anchor = proxy.plotFrame {
                                    let y = loc.y - geo[anchor].origin.y
                                    if let cat: String = proxy.value(atY: y),
                                       let s = items.first(where: { $0.stock == cat }) {
                                        hoverStock = s; hoverSLoc = loc
                                    }
                                } else { hoverStock = nil }
                            }
                    }
                }
                .overlay(alignment: .topLeading) {
                    if let s = hoverStock {
                        tooltip(title: s.stock,
                                value: "\(s.count) sales · \(s.total.money)")
                            .offset(x: max(0, hoverSLoc.x - 36), y: max(0, hoverSLoc.y - 44))
                    }
                }
                .frame(height: max(220, CGFloat(items.count) * 28))
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassCard()
    }

    // MARK: glass chart materials — same Liquid-Glass family as the pills/buttons

    /// Glossy vertical glass gradient for a stock color — stacked timeline bars.
    /// Semi-transparent top→bottom so the glass card behind shows through.
    private func glassBar(_ color: Color) -> LinearGradient {
        LinearGradient(colors: [color.opacity(0.90), color.opacity(0.52)],
                       startPoint: .top, endPoint: .bottom)
    }

    /// Horizontal glass variant for the by-stock bars (brightens on hover).
    private func glassBarH(_ color: Color, active: Bool) -> LinearGradient {
        LinearGradient(colors: [color.opacity(active ? 1.0 : 0.85),
                                color.opacity(active ? 0.72 : 0.48)],
                       startPoint: .leading, endPoint: .trailing)
    }

    /// Thin glass "well" laid under a chart's plot area — the same `glassEffect`
    /// material used by PillSurface, kept faint so the card glass isn't muddied.
    private var plotGlass: some View {
        Color.clear.glassEffect(.regular.tint(.white.opacity(0.04)),
                                in: .rect(cornerRadius: 10))
    }

    // MARK: helpers

    /// Timeline tooltip — bucket total plus the specific stock segment hovered.
    private func timelineTooltip(_ pt: AnalyticsPoint) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(pt.bucket).font(.system(size: 9)).foregroundStyle(model.t3)
            Text(byCount ? "\(pt.count) sales" : pt.total.money)
                .font(.system(size: 11, weight: .bold)).foregroundStyle(model.t1)
            if let seg = hoverSeg {
                HStack(spacing: 5) {
                    Circle().fill(model.color(for: seg.stock)).frame(width: 7, height: 7)
                    Text(seg.stock).font(.system(size: 9)).foregroundStyle(model.t2)
                    Text(byCount ? "\(Int(seg.value))" : seg.value.money)
                        .font(.system(size: 9, weight: .semibold)).foregroundStyle(model.t1)
                }
            }
        }
        .padding(.horizontal, 8).padding(.vertical, 5)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 6))
        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(model.accent.opacity(0.5)))
        .allowsHitTesting(false)
    }

    /// Map a hover position over the donut to its sector (clockwise from top).
    /// Mid-angle (radians, 0 = 3 o'clock, clockwise in screen space) of a slice,
    /// matching the Canvas drawing that starts at -π/2 (12 o'clock).
    private func sliceMidAngle(for target: AnalyticsStock, items: [AnalyticsStock],
                               total: Double) -> Double {
        var start = -Double.pi / 2
        for s in items {
            let v = byCount ? Double(s.count) : s.total
            let sweep = total > 0 ? v / total * 2 * .pi : 0
            if s.id == target.id { return start + sweep / 2 }
            start += sweep
        }
        return start
    }

    /// Hit-test the hand-drawn donut: find which slice the cursor is over.
    private func donutHover(_ phase: HoverPhase, center c: CGPoint,
                            rOuter: CGFloat, rInner: CGFloat,
                            items: [AnalyticsStock], total: Double) -> AnalyticsStock? {
        guard case .active(let loc) = phase, total > 0 else { return nil }
        let dx = Double(loc.x - c.x), dy = Double(loc.y - c.y)
        let r = (dx * dx + dy * dy).squareRoot()
        guard r <= Double(rOuter) && r >= Double(rInner) else { return nil }  // inside ring
        var a = atan2(dy, dx) + .pi / 2          // 0 at 12 o'clock, clockwise
        if a < 0 { a += 2 * .pi }
        let target = a / (2 * .pi) * total
        var cum = 0.0
        for s in items {
            let v = byCount ? Double(s.count) : s.total
            if target >= cum && target < cum + v { hoverDLoc = loc; return s }
            cum += v
        }
        return nil
    }

    private func tooltip(title: String, value: String) -> some View {
        VStack(spacing: 2) {
            Text(title).font(.system(size: 9)).foregroundStyle(model.t3)
            Text(value).font(.system(size: 11, weight: .bold)).foregroundStyle(model.t1)
        }
        .padding(.horizontal, 8).padding(.vertical, 5)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 6))
        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(model.accent.opacity(0.5)))
        .allowsHitTesting(false)
    }

    private func fmt(_ d: Date) -> String {
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"; return f.string(from: d)
    }

    /// Trim the bucket key to a compact axis label per granularity.
    private func label(_ bucket: String) -> String {
        switch data.granularity {
        case "hour":  return String(bucket.suffix(2)) + "h"     // "13h"
        case "day":   return String(bucket.suffix(5))           // "06-15"
        case "month": return bucket                              // "2026-06"
        default:      return bucket                              // "2026"
        }
    }
}
