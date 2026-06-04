import SwiftUI

/// Row of period stat cards. No own GlassEffectContainer — the parent wraps the
/// whole top region in one so the cards reflect into the tab bar below.
struct StatsRow: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        HStack(spacing: 10) {
            ForEach(Period.allCases) { p in
                StatCard(period: p,
                         block: model.statBlock(p),
                         active: model.period == p)
                    .onTapGesture {
                        withAnimation(.snappy) { model.period = p }
                    }
            }
        }
    }
}

struct StatCard: View {
    let period: Period
    let block: StatBlock
    let active: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(period.short)
                .font(.system(size: 9, weight: .bold))
                .tracking(0.8)
                .foregroundStyle(Theme.t3)
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(block.total.money)
                    .font(.system(size: 18, weight: .bold))
                    .foregroundStyle(.primary)
                if let d = block.delta, d > 0.005 {
                    Text("+\(d.money)")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(Theme.green)
                }
            }
            Text("↓\(block.count.formatted())")
                .font(.system(size: 10))
                .foregroundStyle(Theme.t2)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 14).padding(.vertical, 11)
        .glassCard(16)
        .overlay {
            if active {
                RoundedRectangle(cornerRadius: 16)
                    .strokeBorder(Theme.accent, lineWidth: 1.5)
            }
        }
    }
}
