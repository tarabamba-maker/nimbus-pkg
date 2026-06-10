import SwiftUI

struct RootView: View {
    @Environment(AppModel.self) private var model

    private let tabs = ["Downloads", "Best Sellers", "Groups", "Browser"]
    @State private var showSettings = false
    @State private var showLab = false
    @AppStorage("showLabButton") private var showLabButton = false

    var body: some View {
        ZStack {
            AppBackground()

            if !model.ready {
                BootView()
            } else {
                let s = model.surfaces
                VStack(spacing: 14) {
                    topRegion
                    content
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .surfaceMat(s.mat(.scrollPanelBg, isDark: model.isDark),
                                    interactive: s.interactive(.scrollPanelBg),
                                    radius: s.scrollPanelRadius)
                        .offset(s.off(.content))
                }
                .padding(.horizontal, s.contentPaddingH)
                .padding(.vertical, s.contentPaddingV)
                .transition(.opacity)
            }
        }
        // ONE global blur over the WHOLE app for ANY popup (settings, group, photo).
        // Overlays are added AFTER this, so the popups themselves stay sharp. This is
        // why every popup dims/blurs the entire window identically (incl. topbar/tabs),
        // not just the scroll canvas.
        .blur(radius: globalBlur)
        .animation(.snappy(duration: 0.22), value: showSettings)
        .animation(.snappy(duration: 0.22), value: model.groupModal)
        .animation(.snappy(duration: 0.22), value: model.photoPopup)
        .animation(.smooth(duration: 0.25), value: model.ready)
        .preferredColorScheme(model.isDark ? .dark : .light)
        .overlay {
            if showSettings {
                ZStack {
                    BokehDim(dimAlpha: model.surfaces.popupDimAlpha).ignoresSafeArea()
                        .onTapGesture { showSettings = false }
                    SettingsView(showLabButton: $showLabButton,
                                 onShowLab: { showSettings = false; showLab = true },
                                 onClose: { showSettings = false }).environment(model)
                        .transition(.scale(scale: 0.96).combined(with: .opacity))
                }
                .animation(.snappy(duration: 0.22), value: showSettings)
            }
        }
        // Group detail — layer 1. Blurs itself when a photo popup sits on top of it.
        .overlay {
            if let g = model.groupModal {
                ZStack {
                    BokehDim(dimAlpha: model.surfaces.popupDimAlpha).ignoresSafeArea()
                        .onTapGesture { model.groupModal = nil }
                    GroupModal(group: g, onClose: { model.groupModal = nil })
                        .environment(model)
                        .blur(radius: model.photoPopup != nil ? model.surfaces.popupBlurRadius : 0)
                        .transition(.scale(scale: 0.96).combined(with: .opacity))
                }
                .animation(.snappy(duration: 0.22), value: model.groupModal)
                .animation(.snappy(duration: 0.22), value: model.photoPopup)
            }
        }
        // Photo detail — top layer. The SAME PhotoPopup everywhere (cards + group
        // photos), centered over the whole app above an open group modal.
        .overlay {
            if let d = model.photoPopup {
                ZStack {
                    BokehDim(dimAlpha: model.surfaces.popupDimAlpha).ignoresSafeArea()
                        .onTapGesture { model.photoPopup = nil }
                    PhotoPopup(assetID: d.assetID, thumb: d.thumb, byStock: d.byStock,
                               onClose: { model.photoPopup = nil }).environment(model)
                        .transition(.scale(scale: 0.96).combined(with: .opacity))
                }
                .animation(.snappy(duration: 0.22), value: model.photoPopup)
            }
        }
        .overlay(alignment: .trailing) {
            if showLab {
                MaterialsLab(onClose: { showLab = false })
                    .environment(model)
                    .transition(.move(edge: .trailing).combined(with: .opacity))
                    .animation(.snappy(duration: 0.28), value: showLab)
            }
        }
        .animation(.snappy(duration: 0.28), value: showLab)
    }

    private var bindingTab: Binding<Int> {
        Binding(get: { model.activeTab }, set: { model.activeTab = $0 })
    }

    /// One blur amount for the whole app, driven by whichever popup is open.
    private var globalBlur: CGFloat {
        if showSettings { return model.surfaces.settingsBlurRadius }
        if model.groupModal != nil || model.photoPopup != nil { return model.surfaces.popupBlurRadius }
        return 0
    }

    /// Title + stats + tab bar share one GlassEffectContainer so the Liquid Glass
    /// shapes reflect/bleed into each other (e.g. the blue tab tints its neighbours).
    @ViewBuilder private var topRegion: some View {
        let s = model.surfaces
        VStack(spacing: 12) {
            TopBar(showSettings: $showSettings, showLab: $showLab, showLabButton: showLabButton)
                .offset(s.off(.topBar))
            StatsRow()
                .offset(s.off(.statsRow))
            GlassTabBar(tabs: tabs, selection: bindingTab)
                .offset(s.off(.tabBarRow))
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
                // Optional material layer over the gradient/photo (Materials Lab).
                Color.clear
                    .surfaceMat(model.surfaces.mat(.appBackground, isDark: model.isDark),
                                interactive: false, radius: 0)
                    .frame(width: geo.size.width, height: geo.size.height)
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
    @Binding var showLab: Bool
    var showLabButton: Bool
    @Environment(AppModel.self) private var model

    var body: some View {
        let s = model.surfaces
        HStack {
            // Title — independently positionable / resizable (Materials Lab).
            Text("Stock Aggregator")
                .font(.system(size: s.size("titleSize", 16), weight: .bold))
                .foregroundStyle(
                    LinearGradient(colors: [model.accent, Color(red: 0.37, green: 0.77, blue: 1)],
                                   startPoint: .leading, endPoint: .trailing))
                .offset(s.off(.topBarTitle))
            Spacer()
            // Right-side buttons — independently positionable as a group.
            HStack(spacing: 8) {
                if showLabButton {
                    Button { showLab.toggle() } label: {
                        Image(systemName: "paintbrush.pointed")
                            .font(.system(size: 14)).padding(8)
                            .foregroundStyle(showLab ? model.accent : .white)
                    }
                    .buttonStyle(.plain).glassPill(active: showLab)
                }
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
            .offset(s.off(.topBarButtons))
        }
        .padding(.horizontal, 12).padding(.vertical, 6)
        .surfaceMat(s.mat(.topBar, isDark: model.isDark),
                    interactive: false, radius: s.topBarRadius)
    }
}

