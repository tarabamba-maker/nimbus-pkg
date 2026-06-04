import SwiftUI

struct RootView: View {
    @Environment(AppModel.self) private var model

    private let tabs = ["Downloads", "Best Sellers", "Groups", "Browser"]
    @State private var showSettings = false

    var body: some View {
        ZStack {
            AppBackground()

            if !model.ready {
                BootView()
            } else {
                VStack(spacing: 14) {
                    topRegion
                    content
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
                .padding(20)
                .transition(.opacity)
            }
        }
        .animation(.smooth(duration: 0.25), value: model.ready)
        .preferredColorScheme(model.isDark ? .dark : .light)
        .overlay {
            if showSettings {
                ZStack {
                    Rectangle().fill(.ultraThinMaterial).ignoresSafeArea()
                        .onTapGesture { showSettings = false }
                    SettingsView(onClose: { showSettings = false }).environment(model)
                        .transition(.scale(scale: 0.96).combined(with: .opacity))
                }
                .animation(.snappy(duration: 0.22), value: showSettings)
            }
        }
    }

    private var bindingTab: Binding<Int> {
        Binding(get: { model.activeTab }, set: { model.activeTab = $0 })
    }

    /// Title + stats + tab bar share one GlassEffectContainer so the Liquid Glass
    /// shapes reflect/bleed into each other (e.g. the blue tab tints its neighbours).
    @ViewBuilder private var topRegion: some View {
        VStack(spacing: 12) {
            TopBar(showSettings: $showSettings)
            StatsRow()
            GlassTabBar(tabs: tabs, selection: bindingTab)
        }
    }

    @ViewBuilder private var content: some View {
        switch model.activeTab {
        case 0: DownloadsView()
        case 1: BestSellersView()
        case 2: GroupsView()
        case 3: BrowserView()
        default: EmptyView()
        }
    }
}

/// Background behind all glass — gives the Liquid Glass something colourful to
/// refract/reflect. Later replaceable by a user-imported image per theme.
struct AppBackground: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        GeometryReader { geo in
            ZStack {
                if let img = importedImage {
                    Image(nsImage: img).resizable().aspectRatio(contentMode: .fill)
                        .frame(width: geo.size.width, height: geo.size.height)
                        .clipped()
                } else {
                    gradient
                }
            }
            .frame(width: geo.size.width, height: geo.size.height)
            .clipped()
        }
        .ignoresSafeArea()
        .id(model.bgTick)   // refresh after import
    }

    private var importedImage: NSImage? {
        _ = model.bgTick
        let url = model.backgroundURL
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        return NSImage(contentsOf: url)
    }

    private var gradient: some View {
        ZStack {
            LinearGradient(
                colors: model.isDark
                    ? [Color(red: 0.05, green: 0.09, blue: 0.16), Color(red: 0.08, green: 0.13, blue: 0.20), Color(red: 0.04, green: 0.06, blue: 0.10)]
                    : [Color(red: 0.90, green: 0.93, blue: 0.98), Color(red: 0.85, green: 0.89, blue: 0.96), Color(red: 0.92, green: 0.90, blue: 0.97)],
                startPoint: .topLeading, endPoint: .bottomTrailing)
            RadialGradient(colors: [Theme.accent.opacity(model.isDark ? 0.16 : 0.12), .clear],
                           center: .topLeading, startRadius: 10, endRadius: 420)
            RadialGradient(colors: [Color(red: 0.45, green: 0.25, blue: 0.85).opacity(model.isDark ? 0.12 : 0.08), .clear],
                           center: .bottomTrailing, startRadius: 10, endRadius: 460)
        }
    }
}

private struct BootView: View {
    var body: some View {
        VStack(spacing: 14) {
            ProgressView().controlSize(.large)
            Text("Запуск бекенду…").foregroundStyle(Theme.t2)
        }
    }
}

struct TopBar: View {
    @Binding var showSettings: Bool
    @Environment(AppModel.self) private var model

    var body: some View {
        HStack {
            Text("Stock Aggregator")
                .font(.system(size: 16, weight: .bold))
                .foregroundStyle(
                    LinearGradient(colors: [Theme.accent, Color(red: 0.37, green: 0.77, blue: 1)],
                                   startPoint: .leading, endPoint: .trailing))
            Spacer()
            Button { model.isDark.toggle() } label: {
                Image(systemName: model.isDark ? "sun.max" : "moon")
                    .font(.system(size: 14)).padding(8)
            }
            .buttonStyle(.plain).glassPill()
            Button { showSettings = true } label: {
                Image(systemName: "gearshape").font(.system(size: 14)).padding(8)
            }
            .buttonStyle(.plain).glassPill()
        }
    }
}

/// Segmented control rendered as a single Liquid Glass capsule.
struct GlassTabBar: View {
    let tabs: [String]
    @Binding var selection: Int

    var body: some View {
        HStack(spacing: 4) {
            ForEach(Array(tabs.enumerated()), id: \.offset) { i, name in
                Button {
                    withAnimation(.easeOut(duration: 0.16)) { selection = i }
                } label: {
                    Text(name)
                        .font(.system(size: 12.5, weight: .semibold))
                        .padding(.horizontal, 16).padding(.vertical, 7)
                        .foregroundStyle(selection == i ? .white : .secondary)
                }
                .buttonStyle(.plain)
                .background {
                    if selection == i {
                        Capsule().fill(Theme.accent.gradient)
                            .matchedGeometryEffect(id: "tabsel", in: ns)
                    }
                }
            }
        }
        .padding(4)
        .glassPill()
        .frame(maxWidth: .infinity)
    }

    @Namespace private var ns
}
