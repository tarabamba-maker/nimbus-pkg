import SwiftUI

/// Row of period stat cards. No own GlassEffectContainer — the parent wraps the
/// whole top region in one so the cards reflect into the tab bar below.
struct StatsRow: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        HStack(spacing: 10) {
            ForEach(Period.allCases) { p in
                Button {
                    withAnimation(.snappy) { model.period = p }
                } label: {
                    StatCard(period: p,
                             block: model.statBlock(p),
                             active: model.period == p)
                }
                .buttonStyle(.plain)
                .contentShape(RoundedRectangle(cornerRadius: Theme.radius))
            }
        }
    }
}

struct StatCard: View {
    let period: Period
    let block: StatBlock
    let active: Bool
    @Environment(AppModel.self) private var model

    /// Today → "new this sync" money; all other periods → delta vs prior period.
    private var greenBadge: Double? {
        period == .today ? block.new_total : block.delta
    }

    var body: some View {
        let s = model.surfaces
        VStack(alignment: .leading, spacing: 4) {
            Text(period.short)
                .font(.system(size: 9, weight: .bold))
                .tracking(0.8)
                .foregroundStyle(s.t3(isDark: model.isDark))
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(block.total.money)
                    .font(.system(size: 18, weight: .bold))
                    .foregroundStyle(.primary)
                    .contentTransition(.numericText())        // rolling digits on change
                    .animation(.snappy, value: block.total)
                // Today shows money added THIS sync (always ≥0, reacts to new
                // sales); other periods show the elapsed-matched delta vs prior.
                if let badge = greenBadge, badge > 0.005 {
                    Text("+\(badge.money)")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(Theme.green)
                        .contentTransition(.numericText())
                        .animation(.snappy, value: badge)
                }
            }
            Text("↓\(block.count.formatted())")
                .font(.system(size: 10))
                .foregroundStyle(s.t2(isDark: model.isDark))
                .contentTransition(.numericText())
                .animation(.snappy, value: block.count)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 14).padding(.vertical, 11)
        .surfaceMat(s.mat(.statsBlock, isDark: model.isDark),
                    interactive: s.glassInteractive,
                    radius: s.statsRadius)
        .overlay {
            if active {
                RoundedRectangle(cornerRadius: s.statsRadius)
                    .strokeBorder(model.accent, lineWidth: 1.5)
            }
        }
        .contentShape(RoundedRectangle(cornerRadius: s.statsRadius))
    }
}
